"""
Agent de collecte d'offres d'alternance — stack Data/IA, région Lyon/Auvergne-Rhône-Alpes.
Utilise l'API Algolia de Welcome to the Jungle (publique, sans auth) et des flux RSS.
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger("scraper")

# ── configuration ────────────────────────────────────────────────────────────
ALGOLIA_APP_ID = "CSEKHVMS53"
ALGOLIA_API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
ALGOLIA_INDEX = "wttj_jobs_production_fr"
ALGOLIA_ORG_INDEX = "wk_cms_organizations_production"

LYON_LAT = 45.764
LYON_LNG = 4.8357

GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY")
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX")

RSS_FEEDS = [
    "https://www.maddyness.com/feed/",
    "https://www.lesechos.fr/rss/",
    "https://www.journaldunet.com/actualite/rss",
    "https://www.siecledigital.fr/feed/",
    "https://www.frenchweb.fr/feed/",
    "https://hnrss.org/frontpage",
]

INCUBATOR_REFERENCES = ["h7", "hub612", "la-french-tech", "station-f-job-board", "edtech-france", "greentech-innovation"]

HEADERS_ALGOLIA = {
    "X-Algolia-Application-Id": ALGOLIA_APP_ID,
    "X-Algolia-API-Key": ALGOLIA_API_KEY,
    "Content-Type": "application/json",
    "Referer": "https://www.welcometothejungle.com/",
    "Origin": "https://www.welcometothejungle.com",
}
HEADERS_WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}
TIMEOUT = 30


def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


# ── 1. Welcome to the Jungle via Algolia ─────────────────────────────────────

def _office_matches(offices: list[dict] | None, keywords: list[str]) -> bool:
    if not offices:
        return False
    raw = json.dumps(offices).lower()
    return any(kw in raw for kw in keywords)


def _contract_matches(contract_type: str | None) -> bool:
    if not contract_type:
        return False
    return contract_type.lower() in ("apprenticeship", "internship", "alternance")


def scrape_wttj_algolia() -> list[dict[str, Any]]:
    """Recherche des offres d'alternance Data/IA via l'index Algolia de WTTJ."""
    jobs: list[dict[str, Any]] = []
    session = _session()
    seen: set[str] = set()

    url = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

    queries = [
        ("alternance python lyon", False),
        ("alternance data lyon", False),
        ("alternance intelligence artificielle lyon", False),
        ("apprentissage python lyon", False),
        ("alternance data scientist lyon", False),
        ("alternance python remote", True),
        ("alternance data remote", True),
        ("alternance fullstack remote", True),
        ("apprentissage python remote", True),
        ("alternance python créateur contenu lyon", False),
        ("alternance data creator lyon", False),
        ("alternance data engineer lyon", False),
    ]

    for query, skip_location in queries:
        payload = {
            "params": f"query={query}&hitsPerPage=50"
        }
        try:
            r = session.post(url, headers=HEADERS_ALGOLIA, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            hits = r.json().get("hits", [])
        except Exception as exc:
            log.warning("Algolia query=%s — %s", query, exc)
            continue

        for hit in hits:
            uid = hit.get("objectID", "")
            if uid in seen:
                continue
            seen.add(uid)

            title = (hit.get("name") or "").lower()
            contract_type = hit.get("contract_type", "")
            key_missions = hit.get("key_missions") or []
            if isinstance(key_missions, list):
                key_missions = " ".join(str(k) for k in key_missions)
            description = (hit.get("summary") or "") + " " + key_missions + " " + (hit.get("profile") or "")

            if not _contract_matches(contract_type):
                continue
            if not any(kw in title or kw in description.lower()
                       for kw in ["python", "ia", "data", "intelligence artificielle",
                                  "machine learning", "deep learning", "rag", "llm",
                                  "data scientist", "data engineer", "nlp"]):
                continue

            if not skip_location:
                offices = hit.get("offices", [])
                location_keywords = ["lyon", "villeurbanne", "auvergne", "rhône", "auralp"]
                if not _office_matches(offices, location_keywords):
                    continue

            org = hit.get("organization", {}) or {}
            company_name = org.get("name", "") if isinstance(org, dict) else ""
            company_slug = org.get("slug", "") if isinstance(org, dict) else ""
            job_slug = hit.get("slug", "")

            location_parts = []
            if offices:
                o = offices[0]
                location_parts = [o.get("city", ""), o.get("state", "")]
            location_str = ", ".join(filter(None, location_parts)) or "Lyon"

            url_job = f"https://www.welcometothejungle.com/fr/companies/{company_slug}/jobs/{job_slug}" if company_slug and job_slug else ""

            jobs.append({
                "source": "wttj_algolia",
                "company": company_name or "Inconnu",
                "title": hit.get("name", ""),
                "url": url_job,
                "contract": contract_type,
                "contract_type_names": hit.get("contract_type_names", ""),
                "location": location_str,
                "education_level": hit.get("education_level", ""),
                "experience_level": hit.get("experience_level_minimum", ""),
                "salary": f"{hit.get('salary_minimum', '')}-{hit.get('salary_maximum', '')}".strip("-"),
                "remote": hit.get("remote", ""),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })

        log.info("Algolia «%s» → %d offres retenues", query, len(hits))

    log.info("Algolia WTTJ → %d offres totales (unicums)", len(jobs))
    return jobs


# ── 2. Flux RSS — French Tech / startups ─────────────────────────────────────

_NOISE_PATTERNS = re.compile(
    r"(?i)\b(how to|list of|top \d+|guide|tutorial|accelerators|best practices|"
    r"why you should|what is|review|vs\.|versus|cheat sheet|roadmap)\b"
)


def _is_noise(title: str) -> bool:
    """Ignore les articles blog génériques / tutoriels qui ne sont pas des actus entreprises."""
    return bool(_NOISE_PATTERNS.search(title))


def _parse_rss_items(url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    session = _session()
    try:
        r = session.get(url, headers=HEADERS_WEB, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        log.warning("RSS %s — %s", url, exc)
        return items

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as exc:
        log.warning("RSS %s — erreur XML: %s", url, exc)
        return items

    channel = root.find("channel") or root

    for item in channel.findall(".//item"):
        title = item.findtext("title", "")
        link = item.findtext("link", "")
        desc = item.findtext("description", "") or ""
        combined = (title + " " + desc).lower()

        if not any(kw in combined for kw in
                   ["ia", "data", "intelligence artificielle", "machine learning",
                    "startup", "levée", "scale-up", "tech", "numérique"]):
            continue
        if not any(loc in combined for loc in
                   ["lyon", "auvergne", "rhône", "auralp", "lyonnaise", "auvergne-rhône-alpes"]):
            continue
        if _is_noise(title):
            log.debug("RSS bruit filtré : %s", title)
            continue

        items.append({
            "source": "rss",
            "rss_url": url,
            "company": title.strip().split(":")[0].split("-")[0].strip(),
            "title": title.strip(),
            "url": link.strip(),
            "snippet": desc[:300].strip(),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    log.info("RSS %s → %d articles pertinents", url, len(items))
    return items


def scrape_rss_feeds() -> list[dict[str, Any]]:
    all_articles: list[dict[str, Any]] = []
    for feed in RSS_FEEDS:
        all_articles.extend(_parse_rss_items(feed))
    return all_articles


# ── 3. JobTeaser — endpoint JSON, fallback regex HTML ──────────────────────

def _jt_try_json(query: str) -> list[dict[str, Any]] | None:
    """Tente l'endpoint JSON interne de JobTeaser."""
    url = "https://www.jobteaser.com/fr/job-offers"
    params = {"q": query, "contract[]": "apprenticeship", "format": "json"}
    session = _session()
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data.get("offers") or data.get("results") or data.get("data") or None
    except Exception:
        return None


def _extract_jt_offers_html(html_content: str, query: str) -> list[dict[str, Any]]:
    """Extrait les offres du HTML JobTeaser par regex (robuste, pas de DOM)."""
    results: list[dict[str, Any]] = []
    # Pattern 1 : <a href="/fr/job-offer/..."><h3>TITRE</h3></a>
    blocks = re.findall(
        r'href=["\'](/fr/job-offer/[^"\'<>]+)["\'][^>]*>.*?<h3[^>]*>(.*?)</h3>',
        html_content, re.DOTALL | re.IGNORECASE
    )
    seen_urls: set[str] = set()
    for url_path, title in blocks:
        full_url = f"https://www.jobteaser.com{url_path}"
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        title_clean = re.sub(r'<[^>]+>', '', title).strip()
        if not any(kw in title_clean.lower() for kw in
                   ["python", "data", "ia", "intelligence", "machine", "deep", "rag", "llm",
                    "data scientist", "data engineer", "nlp", "fullstack", "developpeur"]):
            continue
        results.append({
            "source": "jobteaser",
            "company": "JobTeaser",
            "title": title_clean,
            "url": full_url,
            "contract": "apprenticeship",
            "location": "Lyon",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })
    return results


def scrape_jobteaser() -> list[dict[str, Any]]:
    """Offres d'alternance Data/IA via JobTeaser. JSON endpoint d'abord, fallback regex HTML."""
    queries = ["data lyon", "python lyon", "ia lyon", "data scientist lyon", "data engineer lyon"]
    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    for query in queries:
        offers = _jt_try_json(query)
        if offers and isinstance(offers, list):
            for o in offers:
                url = o.get("url") or o.get("link") or o.get("id", "")
                uid = url if isinstance(url, str) else str(url)
                if uid in seen:
                    continue
                seen.add(uid)
                results.append({
                    "source": "jobteaser",
                    "company": o.get("company_name") or o.get("company", {}).get("name", ""),
                    "title": o.get("title") or o.get("name", ""),
                    "url": uid if uid.startswith("http") else f"https://www.jobteaser.com{uid}",
                    "contract": "apprenticeship",
                    "location": o.get("city") or o.get("location") or "Lyon",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                })
            log.info("JobTeaser JSON «%s» → %d offres", query, len(offers))
        else:
            # Fallback HTML
            session = _session()
            url = "https://www.jobteaser.com/fr/job-offers"
            params = {"q": query, "contract[]": "apprenticeship"}
            try:
                r = session.get(url, params=params, headers=HEADERS_WEB, timeout=TIMEOUT)
                r.raise_for_status()
                items = _extract_jt_offers_html(r.text, query)
                for it in items:
                    uid = it["url"]
                    if uid in seen:
                        continue
                    seen.add(uid)
                    results.append(it)
                log.info("JobTeaser HTML «%s» → %d offres", query, len(items))
            except Exception as exc:
                log.warning("JobTeaser erreur «%s» — %s", query, exc)

    log.info("JobTeaser total → %d offres (unicums)", len(results))
    return results


# ── 4. HackerNews via Algolia — API publique gratuite ──────────────────────

def scrape_hn_algolia() -> list[dict[str, Any]]:
    """Actualités tech/data lyonnaises via l'API publique HN Algolia (0 clé)."""
    url = "https://hn.algolia.com/api/v1/search"
    queries = ["Lyon data", "Lyon IA", "Lyon startup", "Lyon Python"]
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    session = _session()

    for query in queries:
        params = {"query": query, "tags": "story", "hitsPerPage": 20}
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            hits = data.get("hits", [])
        except Exception as exc:
            log.warning("HN Algolia «%s» — %s", query, exc)
            continue

        for hit in hits:
            uid = hit.get("objectID", "")
            if uid in seen:
                continue
            seen.add(uid)
            title = hit.get("title", "")
            link = hit.get("url") or hit.get("story_url") or f"https://news.ycombinator.com/item?id={uid}"
            combined = (title + " " + (hit.get("story_text") or "")).lower()
            if not any(kw in combined for kw in
                       ["ia", "data", "intelligence", "machine learning", "startup", "python",
                        "rag", "llm", "scale-up", "tech", "numérique"]):
                continue
            if not any(loc in combined for loc in
                       ["lyon", "auvergne", "rhône", "auralp"]):
                continue
            if _is_noise(title):
                log.debug("HN bruit filtré : %s", title)
                continue
            results.append({
                "source": "hn_algolia",
                "company": title.split(":")[0].split("-")[0].strip(),
                "title": title,
                "url": link,
                "snippet": (hit.get("story_text") or "")[:300],
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })

    log.info("HN Algolia → %d articles pertinents", len(results))
    return results


# ── 5. LinkedIn posts via Google Custom Search (gratuit : 100 req/j) ──────

def scrape_linkedin_posts() -> list[dict[str, Any]]:
    """Posts LinkedIn recrutement/data/IA via Google CSE. 100 requêtes/jour gratuites."""
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        log.warning("GOOGLE_CSE_API_KEY ou GOOGLE_CSE_CX manquant — skip LinkedIn posts")
        return []

    url = "https://www.googleapis.com/customsearch/v1"
    queries = [
        "site:linkedin.com/posts/ hiring Lyon data",
        "site:linkedin.com/posts/ recrute Lyon alternance data",
        "site:linkedin.com/posts/ recrute Lyon IA alternance",
        "site:linkedin.com/posts/ recrute Lyon Python alternance",
    ]
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    session = _session()

    for q in queries:
        params = {"key": GOOGLE_CSE_API_KEY, "cx": GOOGLE_CSE_CX, "q": q, "num": 5, "dateRestrict": "d3"}
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            items = r.json().get("items", [])
        except Exception as exc:
            log.warning("Google CSE «%s» — %s", q, exc)
            continue

        for item in items:
            link = item.get("link", "")
            if link in seen:
                continue
            seen.add(link)
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            results.append({
                "source": "linkedin_posts",
                "company": title.split(" on LinkedIn")[0].strip(),
                "title": title,
                "url": link,
                "snippet": snippet[:300],
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })

    log.info("LinkedIn posts → %d résultats", len(results))
    return results


# ── 6. Annuaire entreprises (WTTJ Directory) ────────────────────────────────

def _query_orgs(params: str) -> list[dict[str, Any]]:
    url = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_ORG_INDEX}/query"
    session = _session()
    all_hits: list[dict[str, Any]] = []
    page = 0
    while True:
        full_params = f"{params}&page={page}"
        try:
            r = session.post(url, headers=HEADERS_ALGOLIA, json={"params": full_params}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            hits = data.get("hits", [])
            all_hits.extend(hits)
            nb_pages = data.get("nbPages", 1)
            page += 1
            if page >= nb_pages:
                break
        except Exception as exc:
            log.warning("Algolia orgs page %d — %s", page, exc)
            break
    return all_hits


def _normalize_company(hit: dict[str, Any], source_label: str) -> dict[str, Any]:
    offices = hit.get("offices", [])
    location_parts = []
    if offices:
        o = offices[0]
        location_parts = [o.get("city", ""), o.get("state", "")]
    location_str = ", ".join(filter(None, location_parts)) or "Lyon"
    slug = hit.get("slug", "")
    company_url = f"https://www.welcometothejungle.com/fr/companies/{slug}" if slug else ""
    sectors_raw = hit.get("sectors_name", {})
    sectors = sectors_raw.get("fr", []) if isinstance(sectors_raw, dict) else []
    tools_raw = hit.get("tools_name", {})
    tools = tools_raw.get("data", []) if isinstance(tools_raw, dict) else []
    desc = hit.get("descriptions", {}).get("fr", "") or hit.get("descriptions", {}).get("en", "") or ""
    return {
        "source": source_label,
        "company": hit.get("name", ""),
        "title": hit.get("name", ""),
        "url": company_url,
        "slug": slug,
        "size": hit.get("size", {}).get("fr", ""),
        "nb_employees": hit.get("nb_employees"),
        "sectors": sectors,
        "location": location_str,
        "description": desc[:500],
        "tools": tools,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def _scrape_wttj_directory() -> list[dict[str, Any]]:
    """Méthode A : Annuaire WTTJ — 15-50 employés, Lyon métropole."""
    params = (
        f"query=&hitsPerPage=50&aroundLatLng={LYON_LAT},{LYON_LNG}"
        f"&aroundRadius=10000&filters=size.fr:'Entre 15 et 50 salariés'"
    )
    hits = _query_orgs(params)
    companies = [_normalize_company(h, "wttj_directory") for h in hits]
    log.info("WTTJ Directory (15-50, Lyon) → %d entreprises", len(companies))
    return companies


# ── 7. Incubateurs lyonnais (Méthode B) ────────────────────────────────────

def _scrape_incubators() -> list[dict[str, Any]]:
    """Méthode B : entreprises taguées incubateur + bureau à Lyon."""
    seen_slugs: set[str] = set()
    companies: list[dict[str, Any]] = []

    for ref in INCUBATOR_REFERENCES:
        params = (
            f"query=&hitsPerPage=50&aroundLatLng={LYON_LAT},{LYON_LNG}"
            f"&aroundRadius=20000&filters=website.reference:{ref}"
        )
        hits = _query_orgs(params)
        for h in hits:
            slug = h.get("slug", "")
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            companies.append(_normalize_company(h, f"incubator_{ref}"))

    log.info("Incubateurs Lyon → %d entreprises (unicums)", len(companies))
    return companies


def scrape_directories() -> list[dict[str, Any]]:
    """Collecte des entreprises cibles via annuaire WTTJ + incubateurs."""
    log.info("Lancement du scraping d'annuaires…")
    results: list[dict[str, Any]] = []
    results.extend(_scrape_wttj_directory())
    results.extend(_scrape_incubators())
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for c in results:
        slug = c.get("slug", "")
        if slug in seen:
            continue
        seen.add(slug)
        deduped.append(c)
    log.info("Annuaire terminé — %d entreprises (dédoublonnées)", len(deduped))
    return deduped


# ── 8. Point d'entrée unique ────────────────────────────────────────────────

def run() -> list[dict[str, Any]]:
    log.info("Lancement de la collecte…")
    results: list[dict[str, Any]] = []
    results.extend(scrape_wttj_algolia())
    results.extend(scrape_jobteaser())
    results.extend(scrape_rss_feeds())
    results.extend(scrape_hn_algolia())
    results.extend(scrape_linkedin_posts())
    log.info("Collecte terminée — %d résultats au total", len(results))
    return results


if __name__ == "__main__":
    data = run()
    print(json.dumps(data, indent=2, ensure_ascii=False))
