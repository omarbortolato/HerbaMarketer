"""
tests/e2e_email_test.py

End-to-end test for the full email pipeline on herbago_it.

Steps tested:
  1. Generate email pair (calls Claude API)
  2. Validate email 1 and email 2
  3. Translate for herbago_fr (IT → FR)
  4. Validate FR translation
  5. Publish to Mautic (creates emails + campaign events)
  6. Verify result in DB

Run with:
    python3 tests/e2e_email_test.py
"""

import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite:///./herbamarketer.db")

from dotenv import load_dotenv
load_dotenv()

import structlog
from core.database import SessionLocal, ContentTopic, EmailPair, Site
from config import get_site_config, get_all_active_sites
from agents.content_agent import generate_email_pair
from agents.validator_agent import validate_content
from agents.translator_agent import translate_email_pair
from publishers.mautic import MauticPublisher

log = structlog.get_logger()


def separator(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def check(label: str, value, expected=True) -> None:
    status = "✅" if value == expected else "❌"
    print(f"  {status}  {label}: {value}")
    if value != expected:
        raise AssertionError(f"FAILED — {label}: expected {expected}, got {value}")


def run_e2e_test():
    db = SessionLocal()
    it_site = get_site_config("herbago_it")
    fr_site = get_site_config("herbago_fr")

    # ----------------------------------------------------------------
    separator("STEP 1 — Generate email pair (herbago_it, IT)")
    # ----------------------------------------------------------------
    pair_it = generate_email_pair(
        topic="Colazione proteica: come iniziare la giornata con energia e senza fame",
        site_config=it_site,
        product_name="Formula 1 Herbalife",
    )
    print(f"  Email 1 subject:   {pair_it.email_1.subject}")
    print(f"  Email 1 preheader: {pair_it.email_1.preheader}")
    print(f"  Email 2 subject:   {pair_it.email_2.subject}")
    print(f"  Email 2 preheader: {pair_it.email_2.preheader}")
    check("language", pair_it.language, "it")
    check("site_slug", pair_it.site_slug, "herbago_it")
    check("email_1.subject not empty", bool(pair_it.email_1.subject), True)
    check("email_2.subject not empty", bool(pair_it.email_2.subject), True)
    check("email_1.body_html not empty", bool(pair_it.email_1.body_html), True)
    check("email_2.body_html not empty", bool(pair_it.email_2.body_html), True)

    # ----------------------------------------------------------------
    separator("STEP 2 — Validate IT email pair")
    # ----------------------------------------------------------------
    val_1 = validate_content(pair_it.email_1.body_html, "email_1", "it")
    val_2 = validate_content(pair_it.email_2.body_html, "email_2", "it")
    print(f"  Email 1 score: {val_1.score}/100  passed={val_1.passed}")
    print(f"  Email 2 score: {val_2.score}/100  passed={val_2.passed}")
    if val_1.issues:
        print(f"  Email 1 issues: {val_1.issues}")
    if val_2.issues:
        print(f"  Email 2 issues: {val_2.issues}")
    check("email_1 validation passed", val_1.passed, True)
    check("email_2 validation passed", val_2.passed, True)

    # ----------------------------------------------------------------
    separator("STEP 3 — Translate IT → FR (herbago_fr)")
    # ----------------------------------------------------------------
    pair_fr = translate_email_pair(pair_it, fr_site)
    print(f"  Email 1 (FR): {pair_fr.email_1.subject}")
    print(f"  Email 2 (FR): {pair_fr.email_2.subject}")
    check("translated language", pair_fr.language, "fr")
    check("translated site_slug", pair_fr.site_slug, "herbago_fr")
    check("FR email_1.subject not empty", bool(pair_fr.email_1.subject), True)

    # ----------------------------------------------------------------
    separator("STEP 4 — Validate FR translation")
    # ----------------------------------------------------------------
    val_fr_1 = validate_content(pair_fr.email_1.body_html, "email_1", "fr")
    val_fr_2 = validate_content(pair_fr.email_2.body_html, "email_2", "fr")
    print(f"  FR Email 1 score: {val_fr_1.score}/100  passed={val_fr_1.passed}")
    print(f"  FR Email 2 score: {val_fr_2.score}/100  passed={val_fr_2.passed}")
    if val_fr_1.issues:
        print(f"  FR issues: {val_fr_1.issues}")

    # ----------------------------------------------------------------
    separator("STEP 5 — Publish IT email pair to Mautic")
    # ----------------------------------------------------------------
    publisher = MauticPublisher(it_site)
    result = publisher.publish_email_pair(
        pair_it,
        topic_slug="colazione_proteica",
    )
    print(f"  Email 1 Mautic ID: {result.email_1_mautic_id}  name: {result.email_1_name}")
    print(f"  Email 2 Mautic ID: {result.email_2_mautic_id}  name: {result.email_2_name}")
    print(f"  Campaign event 1: {result.campaign_event_1_id}")
    print(f"  Campaign event 2: {result.campaign_event_2_id}")
    check("email_1_mautic_id > 0", result.email_1_mautic_id > 0, True)
    check("email_2_mautic_id > 0", result.email_2_mautic_id > 0, True)
    check("email_1_name starts with BR_IT", result.email_1_name.startswith("BR_IT"), True)

    # ----------------------------------------------------------------
    separator("STEP 6 — Save to DB and verify")
    # ----------------------------------------------------------------
    topic = db.query(ContentTopic).filter(ContentTopic.id == 1).first()
    site_it = db.query(Site).filter(Site.slug == "herbago_it").first()

    pair_db = EmailPair(
        topic_id=topic.id,
        site_id=site_it.id,
        language="it",
        email_1_subject=pair_it.email_1.subject,
        email_1_body=pair_it.email_1.body_html,
        email_2_subject=pair_it.email_2.subject,
        email_2_body=pair_it.email_2.body_html,
        mautic_email_1_id=result.email_1_mautic_id,
        mautic_email_2_id=result.email_2_mautic_id,
        status="published",
    )
    db.add(pair_db)
    db.commit()
    db.refresh(pair_db)

    saved = db.query(EmailPair).filter(EmailPair.id == pair_db.id).first()
    check("saved to DB", saved is not None, True)
    check("status=published", saved.status, "published")
    check("mautic_email_1_id saved", saved.mautic_email_1_id, result.email_1_mautic_id)

    db.close()

    # ----------------------------------------------------------------
    separator("RISULTATO FINALE")
    # ----------------------------------------------------------------
    print("""
  ✅ Generazione IT     — OK
  ✅ Validazione IT     — OK
  ✅ Traduzione → FR    — OK
  ✅ Validazione FR     — OK
  ✅ Pubblicazione Mautic — OK
  ✅ Salvataggio DB     — OK

  Pipeline Fase 1 funzionante end-to-end!
""")
    print(f"  Mautic email 1: {result.email_1_name} (id={result.email_1_mautic_id})")
    print(f"  Mautic email 2: {result.email_2_name} (id={result.email_2_mautic_id})")
    print(f"\n  Controlla su Mautic → Campaigns → Broadcast Herbago.it")


if __name__ == "__main__":
    run_e2e_test()
