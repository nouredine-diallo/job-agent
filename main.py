"""
Orchestrateur JobAgent.
1. Connecte Google Sheets (mémoire des offres/signaux déjà traités).
2. Scrape les opportunités → analyse → finder → emailer → Telegram.
3. Sauvegarde chaque opportunité traitée dans Sheets.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import gspread
import yaml
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("main")

AUDIT_MODE = False  # ← Met False pour activer les vrais envois SMTP
audit_trail: list[dict[str, Any]] = []

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "data", "profile.yml")
SHEET_NAME = "JobAgent_Memory"
SHEET_OFFERS = "Processed_Offers"
SHEET_SIGNALS = "Processed_Signals"


# ── Google Sheets ────────────────────────────────────────────────────────────

def _gspread_client() -> gspread.Client:
    creds_json = os.getenv("GSPREAD_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("GSPREAD_CREDENTIALS manquante dans .env")
    creds = json.loads(creds_json)
    return gspread.service_account_from_dict(creds)


def _ensure_sheet(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=10)
        ws.append_row(["identifiant", "titre", "source", "score", "statut", "traite_le"])
        return ws


def load_memory() -> tuple[set[str], set[str], set[str], dict[str, str]]:
    """Retourne (offers_seen, signals_seen, companies_seen, bump_dates).
    Les entrées de plus de 14 jours sont ignorées. bump_dates = {id: traite_le}."""
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    try:
        client = _gspread_client()
        sh = client.open(SHEET_NAME)
    except Exception as exc:
        log.warning("Impossible d'ouvrir le Google Sheet (%s) — mémoire vide", exc)
        return set(), set(), set(), {}

    def _load_sheet(sheet_name: str) -> tuple[set[str], dict[str, str]]:
        seen: set[str] = set()
        bumps: dict[str, str] = {}
        try:
            ws = _ensure_sheet(sh, sheet_name)
            rows = ws.get_all_values()
            if rows:
                for row in rows[1:]:
                    if not row or not row[0]:
                        continue
                    date_str = row[5].strip() if len(row) > 5 and row[5].strip() else ""
                    if date_str:
                        try:
                            row_date = datetime.fromisoformat(date_str)
                            if row_date < cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass
                    seen.add(row[0].strip())
                    if date_str:
                        bumps[row[0].strip()] = date_str
        except Exception as exc:
            log.warning("Erreur lecture %s : %s", sheet_name, exc)
        return seen, bumps

    offers_seen, _ = _load_sheet(SHEET_OFFERS)
    signals_seen, _ = _load_sheet(SHEET_SIGNALS)
    companies_seen, company_bumps = _load_sheet(SHEET_COMPANIES)
    bump_dates = {**company_bumps}
    return offers_seen, signals_seen, companies_seen, bump_dates


def mark_processed(
    identifier: str,
    item: dict[str, Any],
    score: int,
    sheet_type: str,
) -> bool:
    try:
        client = _gspread_client()
        sh = client.open(SHEET_NAME)
        sheet_map = {"offer": SHEET_OFFERS, "signal": SHEET_SIGNALS, "company": SHEET_COMPANIES}
        ws = _ensure_sheet(sh, sheet_map.get(sheet_type, SHEET_OFFERS))
        from datetime import datetime, timezone
        ws.append_row([
            identifier,
            item.get("title", ""),
            item.get("source", ""),
            str(score),
            "envoyé",
            datetime.now(timezone.utc).isoformat(),
        ])
        return True
    except Exception as exc:
        log.error("Erreur écriture Sheets pour %s : %s", identifier, exc)
        return False


# ── Workflow helpers ─────────────────────────────────────────────────────────

SHEET_COMPANIES = "Processed_Companies"
SHEET_BUMPS = "Bumps"


def _item_id(item: dict[str, Any]) -> str:
    return item.get("url") or item.get("slug") or item.get("company", "") + "|" + item.get("title", "")


def _format_for_analyzer(item: dict[str, Any], source_type: str) -> str:
    """ Transforme un item scrappé en texte lisible par l'analyzer. """
    parts = [
        f"Titre : {item.get('title', '')}",
        f"Source : {item.get('source', '')}",
    ]
    if source_type == "offer":
        parts += [
            f"Entreprise : {item.get('company', '')}",
            f"Contrat : {item.get('contract', '')}",
            f"Localisation : {item.get('location', '')}",
            f"URL : {item.get('url', '')}",
        ]
    else:
        parts += [
            f"URL : {item.get('url', '')}",
            f"Extrait : {item.get('snippet', '')}",
        ]
    return "\n".join(parts)


def _process_offer(
    offer: dict[str, Any],
    offers_seen: set[str],
) -> bool:
    global audit_trail
    oid = _item_id(offer)
    if oid in offers_seen:
        log.info("Offre déjà traitée : %s", oid)
        return False

    log.info("Nouvelle offre : %s — %s", offer.get("company"), offer.get("title"))

    # ── Analyzer ──────────────────────────────────────────────────────────
    from agents.analyzer import run as analyze
    text = _format_for_analyzer(offer, "offer")
    analysis = analyze(text)
    score = analysis.get("score", 0)
    log.info("Analyse score=%d/10 — %s", score, analysis.get("raison", ""))

    if score < 7:
        log.info("Score < 7 → SKIP")
        mark_processed(oid, offer, score, "offer")
        audit_trail.append({
            "collection": {"method": "Offre", "source": offer.get("source"), "company": offer.get("company"), "title": offer.get("title"), "freshness": offer.get("scraped_at", "")},
            "analyzer_decision": {"score": score, "besoin_primaire": analysis.get("besoin_primaire_identifie", ""), "raisonnement": analysis.get("raison", "")},
            "decision": "skip",
        })
        return False

    # ── Finder ────────────────────────────────────────────────────────────
    from agents.finder import run as find_contact
    company_name = offer.get("company", "Inconnue")
    contact = find_contact(company_name)
    person_found = contact.get("person_found", False)
    is_startup = contact.get("size_estimate") is not None and contact.get("size_estimate") < 50
    roles = ["CTO", "Lead", "Senior", "Tech"] if is_startup else ["Talent Acquisition", "RH", "HR", "Recruitment"]
    serper_query = f'site:linkedin.com/in/ "{company_name}" "Lyon" ({" OR ".join(f"{r}" for r in roles)})'
    log.info("Contact ciblé : %s — %s (trouvé=%s)", contact.get("contact_role"), contact.get("contact_name"), person_found)

    # ── 3-state decision ──────────────────────────────────────────────────
    send_mode = "draft"
    if score > 8 and person_found:
        send_mode = "send"
    if AUDIT_MODE:
        send_mode = "draft"
    log.info("Décision : %s", send_mode)

    # ── Emailer ───────────────────────────────────────────────────────────
    from agents.emailer import run as send_email
    result = send_email(
        source_type="offer",
        source_data=offer,
        contact_info=contact,
        send_mode=send_mode,
    )
    draft = result.get("draft", {})

    audit_trail.append({
        "collection": {"method": "Offre", "source": offer.get("source"), "company": company_name, "title": offer.get("title"), "freshness": offer.get("scraped_at", "")},
        "analyzer_decision": {"score": score, "besoin_primaire": analysis.get("besoin_primaire_identifie", ""), "raisonnement": analysis.get("raison", "")},
        "finder_search": {"serper_query": serper_query, "result": contact.get("contact_name", "Équipe Tech"), "fallback_triggered": not person_found},
        "emailer_draft": {"prompt_type": "Méthode 1 Offre", "subject": draft.get("subject", ""), "body": draft.get("body", ""), "linkedin_note": draft.get("linkedin_note", "")},
        "decision": send_mode,
    })

    mark_processed(oid, offer, score, "offer")
    ok = result.get("telegram_sent", False)
    log.info("Offre traitée (%s) → Telegram %s", send_mode, ok)
    return ok


def _process_signal(
    signal: dict[str, Any],
    signals_seen: set[str],
) -> bool:
    global audit_trail
    sid = _item_id(signal)
    if sid in signals_seen:
        log.info("Signal déjà traité : %s", sid)
        return False

    log.info("Nouveau signal : %s", signal.get("title"))

    # ── Analyzer ──────────────────────────────────────────────────────────
    from agents.analyzer import run as analyze
    text = _format_for_analyzer(signal, "signal")
    analysis = analyze(text)
    score = analysis.get("score", 0)
    log.info("Analyse score=%d/10 — %s", score, analysis.get("raison", ""))

    if score < 7:
        log.info("Score < 7 → SKIP")
        mark_processed(sid, signal, score, "signal")
        audit_trail.append({
            "collection": {"method": "Signal", "source": signal.get("source"), "company": signal.get("company", ""), "title": signal.get("title"), "freshness": signal.get("scraped_at", "")},
            "analyzer_decision": {"score": score, "besoin_primaire": analysis.get("besoin_primaire_identifie", ""), "raisonnement": analysis.get("raison", "")},
            "decision": "skip",
        })
        return False

    # ── 3-state decision ──────────────────────────────────────────────────
    title_combined = (signal.get("title", "") + " " + signal.get("snippet", "")).lower()
    is_fundraising = any(kw in title_combined for kw in ["levée", "fundraising", "million", "tour de table", "raise"])
    send_mode = "send" if is_fundraising else "draft"
    if AUDIT_MODE:
        send_mode = "draft"
    log.info("Décision signal : %s (fundraising=%s)", send_mode, is_fundraising)

    # ── Finder ────────────────────────────────────────────────────────────
    from agents.finder import run as find_contact
    company_name = signal.get("company") or signal.get("title", "Startup")
    contact = find_contact(company_name)
    person_found = contact.get("person_found", False)
    serper_query = f'site:linkedin.com/in/ "{company_name}" "Lyon" ({" OR ".join(f"{r}" for r in ["Talent Acquisition", "RH", "HR", "Recruitment"])})'
    log.info("Contact ciblé : %s — %s", contact.get("contact_role"), contact.get("contact_name"))

    # ── Emailer ───────────────────────────────────────────────────────────
    from agents.emailer import run as send_email
    result = send_email(
        source_type="signal",
        source_data=signal,
        contact_info=contact,
        analysis=analysis,
        send_mode=send_mode,
    )
    draft = result.get("draft", {})

    audit_trail.append({
        "collection": {"method": "Signal", "source": signal.get("source"), "company": company_name, "title": signal.get("title"), "freshness": signal.get("scraped_at", "")},
        "analyzer_decision": {"score": score, "besoin_primaire": analysis.get("besoin_primaire_identifie", ""), "raisonnement": analysis.get("raison", "")},
        "finder_search": {"serper_query": serper_query, "result": contact.get("contact_name", "Équipe Tech"), "fallback_triggered": not person_found},
        "emailer_draft": {"prompt_type": "Méthode XYZ", "subject": draft.get("subject", ""), "body": draft.get("body", ""), "linkedin_note": draft.get("linkedin_note", "")},
        "decision": send_mode,
    })

    mark_processed(sid, signal, score, "signal")
    ok = result.get("telegram_sent", False)
    log.info("Signal traité (%s) → Telegram %s", send_mode, ok)
    return ok


# ── 5. Traitement des entreprises (annuaire / incubateur) ─────────────────────

def _process_company(
    company: dict[str, Any],
    companies_seen: set[str],
) -> bool:
    global audit_trail
    cid = _item_id(company)
    if cid in companies_seen:
        log.info("Entreprise déjà traitée : %s", cid)
        return False

    log.info("Nouvelle entreprise cible : %s (%s employés)", company.get("company"), company.get("nb_employees"))

    # ── Analyzer entreprise ────────────────────────────────────────────────
    from agents.analyzer import analyze_company
    analysis = analyze_company(company)
    score = analysis.get("score", 0)
    log.info("Analyse entreprise score=%d/10 — %s", score, analysis.get("raison", ""))

    if score < 7:
        log.info("Score < 7 → SKIP")
        mark_processed(cid, company, score, "company")
        audit_trail.append({
            "collection": {"method": "Cold", "source": company.get("source"), "company": company.get("company"), "title": company.get("title"), "freshness": company.get("scraped_at", "")},
            "analyzer_decision": {"score": score, "besoin_primaire": analysis.get("besoin_primaire_identifie", ""), "raisonnement": analysis.get("raison", "")},
            "decision": "skip",
        })
        return False

    # ── Finder ─────────────────────────────────────────────────────────────
    from agents.finder import run as find_contact
    company_name = company.get("company", "Inconnue")
    contact = find_contact(
        company_name,
        company_size=company.get("nb_employees"),
    )
    person_found = contact.get("person_found", False)
    is_startup = contact.get("size_estimate") is not None and contact.get("size_estimate") < 50
    roles = ["CTO", "Lead", "Senior", "Tech"] if is_startup else ["Talent Acquisition", "RH", "HR", "Recruitment"]
    serper_query = f'site:linkedin.com/in/ "{company_name}" "Lyon" ({" OR ".join(f"{r}" for r in roles)})'
    log.info("Contact ciblé : %s — %s", contact.get("contact_role"), contact.get("contact_name"))

    # ── News enrichment (Serper, 1 requête) ────────────────────────────────
    from agents.finder import search_company_news
    news_snippet = search_company_news(company_name)
    if news_snippet:
        analysis["news_snippet"] = news_snippet
        log.info("News snippet trouvé pour %s (longueur=%d)", company_name, len(news_snippet))

    # ── Emailer (toujours DRAFT pour le cold) ──────────────────────────────
    send_mode = "draft"
    if AUDIT_MODE:
        send_mode = "draft"
    from agents.emailer import run as send_email
    result = send_email(
        source_type="cold",
        source_data=company,
        contact_info=contact,
        analysis=analysis,
        send_mode=send_mode,
    )
    draft = result.get("draft", {})

    audit_trail.append({
        "collection": {"method": "Cold", "source": company.get("source"), "company": company_name, "title": company.get("title"), "freshness": company.get("scraped_at", "")},
        "analyzer_decision": {"score": score, "besoin_primaire": analysis.get("besoin_primaire_identifie", ""), "raisonnement": analysis.get("raison", "")},
        "finder_search": {"serper_query": serper_query, "result": contact.get("contact_name", "Équipe Tech"), "fallback_triggered": not person_found},
        "emailer_draft": {"prompt_type": "Méthode XYZ", "subject": draft.get("subject", ""), "body": draft.get("body", ""), "linkedin_note": draft.get("linkedin_note", "")},
        "decision": send_mode,
    })

    mark_processed(cid, company, score, "company")
    ok = result.get("telegram_sent", False)
    log.info("Entreprise traitée (draft) → Telegram %s", ok)
    return ok


# ── Boucle de Bump (relance J+4 à J+6) ─────────────────────────────────────

def _load_bumped() -> set[str]:
    """Charge les IDs déjà relancés depuis la feuille Bumps."""
    try:
        client = _gspread_client()
        sh = client.open(SHEET_NAME)
        ws = _ensure_sheet(sh, SHEET_BUMPS)
        rows = ws.get_all_values()
        return set(row[0].strip() for row in rows[1:] if row and row[0].strip())
    except Exception as exc:
        log.warning("Erreur lecture Bumps : %s", exc)
        return set()


def _mark_bumped(identifier: str) -> bool:
    try:
        client = _gspread_client()
        sh = client.open(SHEET_NAME)
        ws = _ensure_sheet(sh, SHEET_BUMPS)
        from datetime import datetime, timezone
        ws.append_row([identifier, datetime.now(timezone.utc).isoformat()])
        return True
    except Exception as exc:
        log.error("Erreur marquage bump %s : %s", identifier, exc)
        return False


def _process_bumps(bump_dates: dict[str, str], quota_left: int) -> int:
    """Relance les entreprises envoyées il y a 4-6 jours. Retourne le nombre de bumps envoyés."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    bumped = _load_bumped()
    sent = 0

    # Charge les données des entreprises depuis le sheet Processed_Companies
    try:
        client = _gspread_client()
        sh = client.open(SHEET_NAME)
        ws = _ensure_sheet(sh, SHEET_COMPANIES)
        rows = ws.get_all_values()
    except Exception as exc:
        log.warning("Erreur lecture companies pour bump : %s", exc)
        return 0

    headers = rows[0] if rows else []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        oid = row[0].strip()
        if oid in bumped:
            continue
        date_str = row[5].strip() if len(row) > 5 and row[5].strip() else ""
        if not date_str:
            continue
        try:
            sent_date = datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        days_ago = (now - sent_date).days
        if days_ago < 4 or days_ago > 6:
            continue
        if sent >= quota_left:
            break

        log.info("Bump potentiel : %s (envoi il y a %d jours)", oid, days_ago)

        # Reconstruit partiellement les data de l'entreprise + contact
        company_name = row[1].strip() if len(row) > 1 else ""
        if not company_name:
            continue

        from agents.finder import run as find_contact
        from agents.finder import search_company_news
        contact = find_contact(company_name, company_size=None)
        news = search_company_news(company_name)

        from agents.analyzer import load_profile
        from agents.emailer import generate_bump
        profile = load_profile()
        draft = generate_bump(profile, {"company": company_name, "title": company_name}, "cold")

        from agents.emailer import send_real_email
        to_email = (contact.get("emails") or [None])[0]
        if to_email:
            ok = send_real_email(to_email, draft.get("subject", ""), draft.get("body", ""))
            if ok:
                _mark_bumped(oid)
                sent += 1
                log.info("Bump envoyé à %s", to_email)
        else:
            log.info("Pas d'email pour bump %s — skip", company_name)

    return sent


# ── Rapport d'Audit E2E ─────────────────────────────────────────────────────

def _generate_audit_report(trail: list[dict[str, Any]]) -> None:
    """Imprime le rapport visuel et exporte en JSON."""
    report_lines: list[str] = []
    sep = "=" * 72
    sub = "-" * 72

    report_lines.append("")
    report_lines.append(sep)
    report_lines.append("  ***  RAPPORT D'AUDIT END-TO-END  ***")
    report_lines.append(sep)
    report_lines.append(f"  Date : {datetime.now(timezone.utc).isoformat()}")
    report_lines.append(f"  Items analysés : {len(trail)}")
    report_lines.append(sep)
    report_lines.append("")

    for i, record in enumerate(trail, 1):
        col = record.get("collection", {})
        dec = record.get("analyzer_decision", {})
        fin = record.get("finder_search", {})
        ema = record.get("emailer_draft", {})
        decision = record.get("decision", "?")

        report_lines.append(f"  +- ITEM {i} " + "-" * 57)
        report_lines.append(f"  | [COLLECTION]  : {col.get('method', '?')}  |  Source : {col.get('source', '?')}")
        report_lines.append(f"  | [ENTREPRISE] : {col.get('company', '?')}")
        report_lines.append(f"  | [TITRE]      : {col.get('title', '?')}")
        report_lines.append(f"  | [FRAICHEUR]  : {col.get('freshness', '?')}")
        report_lines.append(f"  | {sub}")
        report_lines.append(f"  | [SCORE]       : {dec.get('score', '?')}/10")
        report_lines.append(f"  | [BESOIN]      : {dec.get('besoin_primaire', '?')}")
        report_lines.append(f"  | [RAISON]      : {dec.get('raisonnement', '?')}")
        if fin:
            report_lines.append(f"  | {sub}")
            report_lines.append(f"  | [REQUETE]     : {fin.get('serper_query', '?')}")
            report_lines.append(f"  | [RESULTAT]    : {fin.get('result', '?')}")
            report_lines.append(f"  | [FALLBACK]    : {'OUI' if fin.get('fallback_triggered') else 'NON'}")
        if ema:
            report_lines.append(f"  | {sub}")
            report_lines.append(f"  | [PROMPT]      : {ema.get('prompt_type', '?')}")
            report_lines.append(f"  | [OBJET]       : {ema.get('subject', '?')}")
            body = ema.get('body', '')
            report_lines.append(f"  | [BODY]        : {body[:200]}{'...' if len(body) > 200 else ''}")
        report_lines.append(f"  | {sub}")
        report_lines.append(f"  | [DECISION]    : {decision.upper()}")
        report_lines.append(f"  +-{' -' * 30} +")
        report_lines.append("")

    report_lines.append(sep)
    report_lines.append("  [OK] AUDIT TERMINE - Aucun SMTP reel declenche")
    report_lines.append(sep)

    report = "\n".join(report_lines)
    print(report)

    # Export JSON
    from datetime import datetime as dt
    ts = dt.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(os.path.dirname(__file__), f"audit_log_{ts}.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "audit_date": datetime.now(timezone.utc).isoformat(),
                "audit_mode": AUDIT_MODE,
                "items": trail,
            }, f, indent=2, ensure_ascii=False)
        log.info("Rapport exporté → %s", json_path)
    except Exception as exc:
        log.error("Erreur export JSON : %s", exc)

    # Telegram
    try:
        from agents.emailer import send_to_telegram
        msg = "🛑 AUDIT TERMINÉ. 3 brouillons générés. Consultez la console ou le JSON pour valider la logique avant de passer AUDIT_MODE à False."
        import requests as req
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            url_tg = f"https://api.telegram.org/bot{token}/sendMessage"
            req.post(url_tg, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=15)
            log.info("Notification audit envoyée sur Telegram")
    except Exception as exc:
        log.warning("Erreur envoi Telegram audit : %s", exc)


# ── Boucle principale ────────────────────────────────────────────────────────

def main() -> int:
    global audit_trail
    log.info("=== JobAgent — Démarrage ===")
    log.info("AUDIT_MODE = %s — aucun SMTP ne sera déclenché", AUDIT_MODE)

    # 1. Mémoire
    offers_seen, signals_seen, companies_seen, bump_dates = load_memory()
    log.info("Mémoire chargée : %d offres, %d signaux, %d entreprises, %d bump_dates", len(offers_seen), len(signals_seen), len(companies_seen), len(bump_dates))

    # 2. Scraper
    from agents.scraper import run as scrape
    log.info("Lancement du scraper…")
    try:
        raw_items = scrape()
    except Exception as exc:
        log.error("Le scraper a échoué : %s", exc)
        return 1

    offers = [i for i in raw_items if i.get("source") in ("wttj_algolia", "jobteaser")]
    signals = [i for i in raw_items if i.get("source") in ("rss", "hn_algolia", "linkedin_posts")]
    log.info("Scraper : %d offres, %d signaux", len(offers), len(signals))

    # ── Boucle d'audit : 1 offre, 1 signal, 1 cold max ─────────────────────
    processed_counts = {"offer": 0, "signal": 0, "cold": 0}

    for offer in offers:
        if processed_counts["offer"] >= 1:
            log.info("Audit quota offer atteint (1), arrêt")
            break
        try:
            ok = _process_offer(offer, offers_seen)
            if ok:
                processed_counts["offer"] += 1
        except Exception as exc:
            log.error("Erreur lors du traitement de l'offre %s : %s", _item_id(offer), exc, exc_info=True)
            continue

    for signal in signals:
        if processed_counts["signal"] >= 1:
            log.info("Audit quota signal atteint (1), arrêt")
            break
        try:
            ok = _process_signal(signal, signals_seen)
            if ok:
                processed_counts["signal"] += 1
        except Exception as exc:
            log.error("Erreur lors du traitement du signal %s : %s", _item_id(signal), exc, exc_info=True)
            continue

    # Cold : chercher une entreprise dans l'annuaire
    if processed_counts["cold"] < 1:
        from agents.scraper import scrape_directories
        log.info("Lancement du scraping d'annuaires…")
        try:
            companies = scrape_directories()
        except Exception as exc:
            log.error("Le scraping d'annuaires a échoué : %s", exc)
            companies = []

        for company in companies:
            if processed_counts["cold"] >= 1:
                break
            try:
                ok = _process_company(company, companies_seen)
                if ok:
                    processed_counts["cold"] += 1
            except Exception as exc:
                log.error("Erreur lors du traitement de l'entreprise %s : %s", _item_id(company), exc, exc_info=True)
                continue

    log.info("Audit terminé : %s", audit_trail)

    # ── Rapport final ──────────────────────────────────────────────────────
    _generate_audit_report(audit_trail)

    log.info("=== JobAgent — Terminé ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
