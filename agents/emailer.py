"""
Génère un brouillon d'email ultra-personnalisé via Groq (Llama 3) selon la source
(offre d'emploi ou actu startup), l'envoie par SMTP, et notifie sur Telegram.
"""

import json
import logging
import os
import random
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger("emailer")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

_70b_calls = 0
_70b_daily_limit = 950


AUDIT_MODE = False  # ← Mettre à False pour activer les vrais envois


def send_real_email(to_email: str, subject: str, body_text: str) -> bool:
    if AUDIT_MODE:
        log.info("[AUDIT] SMTP bypassé pour %s — sujet: %s", to_email, subject)
        return True
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        log.warning("EMAIL_ADDRESS ou EMAIL_PASSWORD manquant — SMTP ignoré")
        return False
    if not to_email:
        log.warning("Aucune adresse destinataire — SMTP ignoré")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = to_email
    msg["Message-ID"] = f"<{int(time.time())}.{random.randint(1000,9999)}@job-agent>"
    msg["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S %z", time.localtime())

    html_body = body_text.replace("\n", "<br>\n")
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(f"<html><body><p>{html_body}</p></body></html>", "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)
        log.info("Email expédié à %s", to_email)

        delay = random.uniform(180, 480)
        log.info("Attente %.0f s avant prochain envoi…", delay)
        time.sleep(delay)
        return True
    except Exception as exc:
        log.error("Erreur SMTP vers %s : %s", to_email, exc)
        return False


def _groq_complete_json(system: str, user: str, temperature: float = 0.4) -> dict[str, str]:
    global _70b_calls
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY manquante — mets-la dans .env")
    if _70b_calls >= _70b_daily_limit:
        log.warning("Budget Groq 70B épuisé")
        return {"subject": "", "body": "", "linkedin_note": ""}
    _70b_calls += 1
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
    }
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
    except Exception as exc:
        log.error("Erreur Groq JSON : %s", exc)
        return {"subject": "", "body": "", "linkedin_note": ""}
    result.setdefault("subject", "")
    result.setdefault("body", "")
    result.setdefault("linkedin_note", "")
    return result


def _build_method1_prompt(profile: dict[str, Any], offer: dict[str, Any]) -> tuple[str, str]:
    system = """Tu rédiges un email de candidature en alternance. Le ton est professionnel-dynamique, direct, sans formules lourdes. Le body doit faire entre 65 et 90 mots, ni plus ni moins.

L'email body doit suivre cette structure exacte :
1. Accroche sur un élément de leur stack tech (ex: "J'ai vu que vous utilisez LangChain en production.")
2. Présentation rapide : étudiant L2 Informatique Lyon, cherche alternance.
3. Preuve de capacité : "Pour vous montrer ce que je sais faire, j'ai déjà en production..." — citer dynamiquement le projet RAG+CI/CD ou le Wrapper TikTok selon ce qui colle le mieux au besoin.
4. Pente d'apprentissage : "Mes compétences évoluent vite, je suis très autonome et prêt à absorber le reste de votre stack."
5. Call to action : proposition d'un call de 10 min.

Réponds UNIQUEMENT avec un objet JSON valide avec ces 3 clés :
- "subject": objet de l'email (concis, accrocheur)
- "body": corps de l'email (pas d'objet, pas de signature, pas de formules RH)
- "linkedin_note": version ultra-courte (< 300 caractères) pour demande de connexion LinkedIn, reprend l'accroche et le CTA"""
    user = f"""Profil candidat : {json.dumps(profile, ensure_ascii=False)}
Offre : {json.dumps(offer, ensure_ascii=False)}
Génère le JSON."""
    return system, user


def _build_xyz_prompt(profile: dict[str, Any], source_data: dict[str, Any], analysis: dict[str, Any]) -> tuple[str, str]:
    news_snippet = (analysis or {}).get("news_snippet", "") or ""
    insertion = ""
    if news_snippet:
        insertion = (
            f"\n\nActualité récente de la cible : {news_snippet}\n"
            f"Intègre CETTE actu dans l'accroche si elle est pertinente (ex: 'Félicitations pour le lancement de…'). "
            f"Si l'actu n'a aucun rapport avec leur métier, ignore-la."
        )

    system = f"""Tu génères un email B2B de prospection alternance. Zéro bla-bla RH. Pas de 'J'espère que vous allez bien'. Pas de formules de politesse. Le body doit faire entre 65 et 90 mots, ni plus ni moins.

Le body doit suivre cette structure obligatoire — Méthode XYZ par Preuve :

[X - L'Observation] : 'J'étudie l'écosystème [Secteur] à Lyon. En analysant votre modèle, j'ai remarqué que le traitement de [douleur déduite de l'analyse] doit être un vrai goulot d'étranglement pour votre équipe.'{insertion}

[Y - La Proposition] : 'Je vous propose de diviser ce temps par 10 en déployant un moteur d'analyse interne sécurisé.'

[Z - La Preuve Technique et le Hack] : 'Je ne suis pas un prestataire classique. Je suis étudiant en L2 Informatique, mais j'ai déjà construit et mis en production [citer le projet RAG évalué MRR ou le Wrapper LLM selon le besoin]. Je cherche une alternance pour construire CE système spécifique chez vous. Cela vous permet d'avoir une R&D IA interne à très faible coût.'

[Call to Action] : 'Est-ce un sujet critique pour vous ce trimestre ? Si oui, voici mon GitHub. On peut s'appeler 10 min la semaine prochaine.'

Interdiction stricte : n'invente aucun nom, aucun projet, aucun détail technique que le candidat n'a pas.

Réponds UNIQUEMENT avec un objet JSON valide avec ces 3 clés :
- "subject": objet de l'email (concis, accrocheur)
- "body": corps de l'email avec la structure XYZ ci-dessus
- "linkedin_note": version ultra-courte (< 300 caractères) pour demande de connexion LinkedIn, reprend l'observation et le CTA"""
    user = f"""Profil candidat : {json.dumps(profile, ensure_ascii=False)}
Source : {json.dumps(source_data, ensure_ascii=False)}
Analyse besoin : {json.dumps(analysis, ensure_ascii=False)}

Génère le JSON."""
    return system, user


def generate_draft(
    profile: dict[str, Any],
    source_data: dict[str, Any],
    source_type: str,
    analysis: dict[str, Any] | None = None,
) -> dict[str, str]:
    if source_type == "offer":
        system, user = _build_method1_prompt(profile, source_data)
    elif source_type in ("signal", "cold"):
        system, user = _build_xyz_prompt(profile, source_data, analysis or {})
    else:
        raise ValueError(f"source_type inconnu : {source_type}")

    result = _groq_complete_json(system, user)
    body = result.get("body", "")
    wc = len(body.split())
    if wc < 65 or wc > 90:
        log.info("Word count=%d hors cible (65-90), retry…", wc)
        result2 = _groq_complete_json(system, user)
        body2 = result2.get("body", "")
        wc2 = len(body2.split())
        if wc2 < 65 or wc2 > 90:
            log.warning("Toujours hors cible après retry (wc=%d), on garde", wc2)
        else:
            result = result2
    return result


def send_to_telegram(company: str, status: str, linkedin_search_url: str, linkedin_note: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant — envoi Telegram ignoré")
        return False

    message = (
        f"📧 *{company}*\n"
        f"✅ {status}\n"
        f"🔗 {linkedin_search_url}\n\n"
        f"💬 *LinkedIn :* {linkedin_note}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        log.info("Notification Telegram envoyée")
        return True
    except Exception as exc:
        log.error("Erreur envoi Telegram : %s", exc)
        return False


def send_to_telegram_draft(
    company: str, draft: dict[str, str], contact_info: dict[str, Any], source_type: str
) -> bool:
    """Envoie le brouillon COMPLET sur Telegram (pas de SMTP)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    mode = "📧 *OFFRE*" if source_type == "offer" else "📧 *COLD*" if source_type == "cold" else "📧 *SIGNAL*"
    contact_name = contact_info.get("contact_name", "?")
    contact_email = (contact_info.get("emails") or [""])[0]
    email_text = (
        f"{mode} — *{company}*\n"
        f"👤 Contact : {contact_name} | {contact_email}\n"
        f"🔗 {contact_info.get('linkedin_search_url', '')}\n\n"
        f"📝 *Objet :* {draft.get('subject', '')}\n\n"
        f"{draft.get('body', '')}\n\n"
        f"💬 *LinkedIn :* {draft.get('linkedin_note', '')}\n\n"
        f"_⚠️ Envoi manuel requis (SMTP désactivé)_"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": email_text, "parse_mode": "Markdown"}, timeout=15)
        r.raise_for_status()
        log.info("Draft Telegram envoyé pour %s", company)
        return True
    except Exception as exc:
        log.error("Erreur envoi draft Telegram : %s", exc)
        return False


def generate_bump(
    profile: dict[str, Any], source_data: dict[str, Any], source_type: str, analysis: dict[str, Any] | None = None
) -> dict[str, str]:
    """Génère un email de relance ultra-court (40 mots max)."""
    context = ""
    if analysis:
        context = f"Contexte de l'analyse : {json.dumps(analysis, ensure_ascii=False)}"
    system = "Tu rédiges un email de relance B2B très court. Maximum 40 mots. Ton différent, pas de répétition. Pas de formule de politesse. JSON valide : {subject, body, linkedin_note}."
    user = (
        f"Profil : {json.dumps(profile, ensure_ascii=False)}\n"
        f"Source : {json.dumps(source_data, ensure_ascii=False)}\n"
        f"{context}\n"
        f"Génère un email de relance ultra-court avec un angle nouveau."
    )
    result = _groq_complete_json(system, user)
    body = result.get("body", "")
    wc = len(body.split())
    if wc > 45:
        log.info("Bump word count=%d, retry…", wc)
        result2 = _groq_complete_json(system, user)
        if len(result2.get("body", "").split()) <= 45:
            result = result2
    return result


def run(
    source_type: str,
    source_data: dict[str, Any],
    contact_info: dict[str, Any],
    profile: dict[str, Any] | None = None,
    send_mode: str = "send",
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if profile is None:
        import yaml
        path = os.path.join(os.path.dirname(__file__), "..", "data", "profile.yml")
        with open(path, encoding="utf-8") as f:
            profile = yaml.safe_load(f)

    draft = generate_draft(profile, source_data, source_type, analysis=analysis)
    result = {
        "draft": draft,
        "contact": contact_info,
        "source_type": source_type,
    }

    company = contact_info.get("company", source_data.get("company", "?"))

    if send_mode == "send":
        to_email = (contact_info.get("emails") or [None])[0]
        email_ok = send_real_email(
            to_email=to_email,
            subject=draft.get("subject", ""),
            body_text=draft.get("body", ""),
        )
        result["email_sent"] = email_ok

        if email_ok:
            tg_ok = send_to_telegram(
                company=company,
                status=f"Email expédié à {to_email}",
                linkedin_search_url=contact_info.get("linkedin_search_url") or contact_info.get("linkedin", ""),
                linkedin_note=draft.get("linkedin_note", ""),
            )
            result["telegram_sent"] = tg_ok
        else:
            result["telegram_sent"] = False
            log.warning("Email non envoyé → pas de notification Telegram")

    elif send_mode == "draft":
        tg_ok = send_to_telegram_draft(company, draft, contact_info, source_type)
        result["telegram_sent"] = tg_ok
        result["email_sent"] = False

    return result


if __name__ == "__main__":
    import sys
    import yaml

    if len(sys.argv) < 2:
        print("Usage : python emailer.py <offer|signal> [chemin_fichier_source.json]")
        sys.exit(1)

    source_type = sys.argv[1]
    source_path = sys.argv[2] if len(sys.argv) > 2 else None

    if source_path:
        with open(source_path, encoding="utf-8") as f:
            source_data = json.load(f)
    else:
        raw = sys.stdin.read()
        source_data = json.loads(raw)

    contact_info = source_data.pop("contact", {
        "company": source_data.get("company", "Inconnue"),
        "contact_role": "CTO",
        "contact_name": "Responsable",
        "emails": ["contact@example.com"],
        "linkedin": "",
    })

    result = run(source_type, source_data, contact_info)
    print(json.dumps(result, indent=2, ensure_ascii=False))
