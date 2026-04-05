"""
core/scheduler.py

APScheduler-based job scheduler for HerbaMarketer.

Jobs:
  - email_job: runs every 15 days (configurable via EMAIL_JOB_INTERVAL_DAYS)
    For each active Mautic site:
      1. Pick next approved topic from backlog
      2. Generate email pair (IT master)
      3. Validate content
      4. Translate for each site language
      5. Validate translations
      6. Publish to Mautic
      7. Notify via Telegram
      8. Log result

Usage:
    from core.scheduler import start_scheduler
    scheduler = start_scheduler()
    # scheduler runs in background threads

Or standalone:
    python -m core.scheduler
"""

import re
import time
from datetime import datetime
from typing import Optional

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agents.content_agent import generate_email_pair, generate_article
from agents.validator_agent import validate_content
from agents.translator_agent import translate_email_pair, translate_article
from config import SiteConfig, get_all_active_sites, get_settings
from core.database import (
    Article,
    ContentTopic,
    EmailPair,
    PublishLog,
    SessionLocal,
    Site,
)
from core.image_generator import generate_image
from core.sitemap import find_equivalent_product_url, find_product_url
from core.telegram_bot import (
    notify_article_drafts_ready,
    notify_brevo_templates_ready,
    notify_email_pair_ready,
    notify_publish_result,
    notify_error,
    notify_topic_selection,
)
from publishers.brevo import BrevoPublisher
from publishers.mautic import MauticPublisher
from publishers.wordpress import WordPressPublisher

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_site_db(db, site_cfg: SiteConfig) -> Site:
    """Ensure the site exists in DB, create if missing."""
    site = db.query(Site).filter(Site.slug == site_cfg.slug).first()
    if not site:
        site = Site(
            slug=site_cfg.slug,
            url=site_cfg.url,
            language=site_cfg.language,
            locale=site_cfg.locale,
            mautic_campaign_id=site_cfg.mautic_campaign_id,
            email_prefix=site_cfg.email_prefix,
            platform=site_cfg.platform,
            active=site_cfg.active,
        )
        db.add(site)
        db.commit()
        db.refresh(site)
        log.info("site_created_in_db", slug=site_cfg.slug)
    return site


def _pick_next_topic_for_email(db) -> Optional[ContentTopic]:
    """Pick the next topic eligible for email generation.
    Eligible: approved (neither job done yet) or article_done (article done, email still pending).
    """
    return (
        db.query(ContentTopic)
        .filter(ContentTopic.status.in_(["approved", "article_done"]))
        .order_by(ContentTopic.priority.desc(), ContentTopic.created_at)
        .first()
    )


def _pick_next_topic_for_article(db) -> Optional[ContentTopic]:
    """Pick the next topic eligible for article generation.
    Eligible: approved (neither job done yet) or email_done (emails done, article still pending).
    """
    return (
        db.query(ContentTopic)
        .filter(ContentTopic.status.in_(["approved", "email_done"]))
        .order_by(ContentTopic.priority.desc(), ContentTopic.created_at)
        .first()
    )


def _topic_to_slug(title: str) -> str:
    """Convert topic title to a short slug for Mautic naming."""
    slug = re.sub(r"[^a-z0-9\s]", "", title.lower())
    slug = re.sub(r"\s+", "_", slug.strip())
    return slug[:40]


def _log_publish(db, entity_type: str, entity_id: int, site_id: int,
                 action: str, detail: str = "") -> None:
    entry = PublishLog(
        entity_type=entity_type,
        entity_id=entity_id,
        site_id=site_id,
        action=action,
        detail=detail,
    )
    db.add(entry)
    db.commit()


def _fix_email_urls(
    translated: "EmailPairOutput",
    master_site: SiteConfig,
    target_site: SiteConfig,
    master_product_url: str,
) -> "EmailPairOutput":
    """
    After translation, replace IT master URLs with target-site URLs:
      - Footer: preferred_customer_url and distributor_url
      - Email 2 CTA: product URL looked up from target site's sitemap

    If the product URL can't be found in the target sitemap, sends a Telegram
    notification asking Omar for the correct URL and falls back to site root.
    """
    replacements: dict[str, str] = {}

    # Footer links
    it_pc = master_site.preferred_customer_url or ""
    tgt_pc = target_site.preferred_customer_url or ""
    if it_pc and tgt_pc and it_pc != tgt_pc:
        replacements[it_pc] = tgt_pc

    it_dist = master_site.distributor_url or ""
    tgt_dist = target_site.distributor_url or ""
    if it_dist and tgt_dist and it_dist != tgt_dist:
        replacements[it_dist] = tgt_dist

    # Product URL in email_2
    if master_product_url:
        target_product_url = find_equivalent_product_url(master_product_url, target_site)
        if target_product_url is None:
            log.warning(
                "product_url_not_found_for_target_site",
                site=target_site.slug,
                fallback=target_site.url,
            )
            notify_error(
                f"Prodotto non trovato — {target_site.slug}",
                f"Non ho trovato l'URL di 'Formula 1 Herbalife' su {target_site.url}.\n"
                f"Inviami il link diretto o il nome esatto del prodotto e aggiorno il link.",
            )
            target_product_url = target_site.url
        if target_product_url != master_product_url:
            replacements[master_product_url] = target_product_url

    if not replacements:
        return translated

    def _apply(text: str) -> str:
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    translated.email_1.body_html = _apply(translated.email_1.body_html)
    translated.email_1.body_text = _apply(translated.email_1.body_text)
    translated.email_2.body_html = _apply(translated.email_2.body_html)
    translated.email_2.body_text = _apply(translated.email_2.body_text)

    log.info(
        "email_urls_fixed",
        target_site=target_site.slug,
        replacements=list(replacements.keys()),
    )
    return translated


def _already_published(db, topic_id: int, site_id: int) -> bool:
    """Idempotency check: return True if this topic/site pair was already published."""
    return (
        db.query(EmailPair)
        .filter(
            EmailPair.topic_id == topic_id,
            EmailPair.site_id == site_id,
            EmailPair.status == "published",
        )
        .first()
        is not None
    )


# ---------------------------------------------------------------------------
# Core email job logic
# ---------------------------------------------------------------------------


def _process_site_email(site_cfg: SiteConfig, topic: ContentTopic, db) -> None:
    """
    Generate, validate, translate, and publish one email pair for a site.
    """
    site_db = _get_or_create_site_db(db, site_cfg)

    # Idempotency
    if _already_published(db, topic.id, site_db.id):
        log.info(
            "email_pair_already_published",
            topic_id=topic.id,
            site=site_cfg.slug,
        )
        return

    log.info("processing_email_job", site=site_cfg.slug, topic_id=topic.id)

    settings = get_settings()
    max_attempts = settings.validator.get("max_regeneration_attempts", 3)

    # --- Generate (IT master) ---
    email_pair = None
    for attempt in range(1, max_attempts + 1):
        try:
            email_pair = generate_email_pair(
                topic=topic.title,
                site_config=site_cfg,
            )
            break
        except Exception as exc:
            log.warning("email_generation_failed", attempt=attempt, error=str(exc))
            if attempt == max_attempts:
                raise

    # --- Validate ---
    val_1 = validate_content(email_pair.email_1.body_html, "email_1", site_cfg.language)
    val_2 = validate_content(email_pair.email_2.body_html, "email_2", site_cfg.language)

    if not val_1.passed or not val_2.passed:
        issues = val_1.issues + val_2.issues
        log.warning("email_pair_validation_failed", issues=issues, site=site_cfg.slug)
        _log_publish(db, "email_pair", 0, site_db.id, "failed",
                     f"Validation failed: {'; '.join(issues)}")
        notify_error(f"Validazione email {site_cfg.slug}", "; ".join(issues))
        return

    # --- Save draft to DB ---
    pair_db = EmailPair(
        topic_id=topic.id,
        site_id=site_db.id,
        language=site_cfg.language,
        email_1_subject=email_pair.email_1.subject,
        email_1_body=email_pair.email_1.body_html,
        email_2_subject=email_pair.email_2.subject,
        email_2_body=email_pair.email_2.body_html,
        status="draft",
    )
    db.add(pair_db)
    db.commit()
    db.refresh(pair_db)

    # --- Notify Telegram for review ---
    notify_email_pair_ready(
        email_pair_id=pair_db.id,
        site_slug=site_cfg.slug,
        topic_title=topic.title,
        email_1_subject=email_pair.email_1.subject,
        email_2_subject=email_pair.email_2.subject,
    )

    # --- Publish to Mautic ---
    try:
        publisher = MauticPublisher(site_cfg)
        topic_slug = _topic_to_slug(topic.title)
        result = publisher.publish_email_pair(email_pair, topic_slug)

        pair_db.mautic_email_1_id = result.email_1_mautic_id
        pair_db.mautic_email_2_id = result.email_2_mautic_id
        pair_db.status = "published"
        pair_db.published_at = datetime.utcnow()
        db.commit()

        _log_publish(db, "email_pair", pair_db.id, site_db.id, "published",
                     f"{result.email_1_name} | {result.email_2_name}")

        notify_publish_result(
            site_slug=site_cfg.slug,
            email_1_name=result.email_1_name,
            email_2_name=result.email_2_name,
            success=True,
        )
        log.info("email_job_complete", site=site_cfg.slug, pair_id=pair_db.id)

    except Exception as exc:
        pair_db.status = "failed"
        db.commit()
        _log_publish(db, "email_pair", pair_db.id, site_db.id, "failed", str(exc))
        notify_publish_result(
            site_slug=site_cfg.slug,
            email_1_name="",
            email_2_name="",
            success=False,
            error=str(exc),
        )
        log.error("mautic_publish_failed", site=site_cfg.slug, error=str(exc))
        raise


def _process_site_email_with_translations(
    master_site: SiteConfig,
    topic: ContentTopic,
    db,
    site_slugs: Optional[list] = None,
) -> None:
    """
    Generate the IT master email pair, then translate and publish
    for each other active Mautic site.
    """
    settings = get_settings()
    max_attempts = settings.validator.get("max_regeneration_attempts", 3)

    # Generate IT master
    master_pair = None
    for attempt in range(1, max_attempts + 1):
        try:
            master_pair = generate_email_pair(
                topic=topic.title,
                site_config=master_site,
                product_url=topic.product_url or None,
            )
            break
        except Exception as exc:
            log.warning("master_generation_failed", attempt=attempt, error=str(exc))
            if attempt == max_attempts:
                raise

    # Validate master
    v1 = validate_content(master_pair.email_1.body_html, "email_1", master_site.language)
    v2 = validate_content(master_pair.email_2.body_html, "email_2", master_site.language)
    if not v1.passed or not v2.passed:
        raise ValueError(f"Master validation failed: {v1.issues + v2.issues}")

    # Process all active sites (Mautic + Brevo), optionally filtered by slug
    all_sites = get_all_active_sites()
    email_sites = [s for s in all_sites if s.platform in ("mautic", "brevo")]
    if site_slugs:
        email_sites = [s for s in email_sites if s.slug in site_slugs]

    for site_cfg in email_sites:
        try:
            site_db = _get_or_create_site_db(db, site_cfg)
            if _already_published(db, topic.id, site_db.id):
                continue

            # Translate if needed
            translated = translate_email_pair(master_pair, site_cfg)

            # Fix URLs: replace IT master product/footer URLs with target site URLs
            translated = _fix_email_urls(
                translated, master_site, site_cfg, master_pair.product_url
            )

            # Validate translation
            tv1 = validate_content(translated.email_1.body_html, "email_1", site_cfg.language)
            tv2 = validate_content(translated.email_2.body_html, "email_2", site_cfg.language)
            if not tv1.passed or not tv2.passed:
                log.warning(
                    "translation_validation_failed",
                    site=site_cfg.slug,
                    issues=tv1.issues + tv2.issues,
                )
                _log_publish(db, "email_pair", 0, site_db.id, "failed",
                             f"Translation validation failed: {tv1.issues + tv2.issues}")
                continue

            # Save draft
            pair_db = EmailPair(
                topic_id=topic.id,
                site_id=site_db.id,
                language=site_cfg.language,
                email_1_subject=translated.email_1.subject,
                email_1_body=translated.email_1.body_html,
                email_2_subject=translated.email_2.subject,
                email_2_body=translated.email_2.body_html,
                status="draft",
            )
            db.add(pair_db)
            db.commit()
            db.refresh(pair_db)

            topic_slug = _topic_to_slug(topic.title)

            # Publish — Mautic or Brevo depending on platform
            if site_cfg.platform == "mautic":
                publisher = MauticPublisher(site_cfg)
                result = publisher.publish_email_pair(translated, topic_slug)
                pair_db.mautic_email_1_id = result.email_1_mautic_id
                pair_db.mautic_email_2_id = result.email_2_mautic_id
                name_1, name_2 = result.email_1_name, result.email_2_name

            else:  # brevo — creates templates, manual addition to automation required
                publisher = BrevoPublisher(site_cfg)
                result = publisher.publish_email_pair(translated, topic_slug)
                # Store Brevo template IDs in mautic fields (schema reuse)
                pair_db.mautic_email_1_id = result.template_1_id
                pair_db.mautic_email_2_id = result.template_2_id
                name_1, name_2 = result.template_1_name, result.template_2_name

            pair_db.status = "published"
            pair_db.published_at = datetime.utcnow()
            db.commit()

            _log_publish(db, "email_pair", pair_db.id, site_db.id, "published",
                         f"{name_1} | {name_2}")

            if site_cfg.platform == "brevo":
                notify_brevo_templates_ready(
                    topic_title=topic.title,
                    template_1_name=name_1,
                    template_1_id=result.template_1_id,
                    template_2_name=name_2,
                    template_2_id=result.template_2_id,
                )
            else:
                notify_publish_result(
                    site_slug=site_cfg.slug,
                    email_1_name=name_1,
                    email_2_name=name_2,
                    success=True,
                )

            # Rate limit between sites
            time.sleep(2)

        except Exception as exc:
            log.error("site_email_job_failed", site=site_cfg.slug, error=str(exc))
            notify_error(f"Email job {site_cfg.slug}", str(exc))
            # Continue with next site, don't abort all


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


def email_job(site_slugs: Optional[list] = None) -> None:
    """
    Main scheduled job: generate and publish one email pair per run,
    translated for all active Mautic sites.

    Triggered every EMAIL_JOB_INTERVAL_DAYS days.

    Args:
        site_slugs: optional list of site slugs to restrict publishing to.
                    If None or empty, all active email sites are used.
    """
    log.info("email_job_started", at=datetime.utcnow().isoformat(), sites=site_slugs or "all")

    db = SessionLocal()
    try:
        topic = _pick_next_topic_for_email(db)
        if not topic:
            log.info("email_job_no_eligible_topics")
            notify_error("Email Job", "Nessun topic in backlog per le email — aggiungi topic con /addtopic")
            return

        original_status = topic.status
        topic.status = "in_progress"
        db.commit()

        it_site = next(
            (s for s in get_all_active_sites() if s.slug == "herbago_it"), None
        )
        if not it_site:
            raise ValueError("herbago_it not found in active sites — check sites.yaml")

        try:
            _process_site_email_with_translations(it_site, topic, db, site_slugs=site_slugs or None)
            # If article was already done, both are done; otherwise mark email as done
            topic.status = "done" if original_status == "article_done" else "email_done"
            db.commit()
            log.info("email_job_finished", topic_id=topic.id, new_status=topic.status)
        except Exception as exc:
            topic.status = original_status  # reset to original so it can be retried
            db.commit()
            log.error("email_job_failed", topic_id=topic.id, error=str(exc))
            notify_error("Email Job", str(exc))

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Article job helpers
# ---------------------------------------------------------------------------


def _already_published_article(db, topic_id: int, site_id: int) -> bool:
    """Return True if an article for this topic/site was already published."""
    return (
        db.query(Article)
        .filter(
            Article.topic_id == topic_id,
            Article.site_id == site_id,
            Article.status.in_(["pending_approval", "published"]),
        )
        .first()
        is not None
    )


def _process_article_for_sites(
    topic: ContentTopic,
    image_url: Optional[str],
    master_article,
    db,
    site_slugs: Optional[list] = None,
) -> list[dict]:
    """
    Translate master article (IT), validate, check product availability,
    publish as WP draft per site. Returns list of {site_slug, post_id, post_url}.
    """
    settings = get_settings()
    all_sites = get_all_active_sites()
    wp_sites = [s for s in all_sites if s.wp_api_url]
    if site_slugs:
        wp_sites = [s for s in wp_sites if s.slug in site_slugs]

    results = []
    for site_cfg in wp_sites:
        try:
            site_db = _get_or_create_site_db(db, site_cfg)

            if _already_published_article(db, topic.id, site_db.id):
                log.info(
                    "article_already_published",
                    topic_id=topic.id,
                    site=site_cfg.slug,
                )
                continue

            # Translate (no-op if same language)
            translated = translate_article(master_article, site_cfg)

            # Fix product URL: replace IT master URL with target site equivalent
            if master_article.product_url:
                target_product_url = find_equivalent_product_url(master_article.product_url, site_cfg)
            else:
                target_product_url = find_product_url("Formula 1 Herbalife", site_cfg)
            if target_product_url is None:
                target_product_url = site_cfg.url
                log.warning(
                    "product_not_available_for_site_using_fallback",
                    site=site_cfg.slug,
                    fallback=target_product_url,
                )
                notify_error(
                    f"Prodotto non trovato — {site_cfg.slug}",
                    f"Non ho trovato l'URL di 'Formula 1 Herbalife' su {site_cfg.url}.\n"
                    f"Inviami il link diretto o il nome esatto del prodotto.",
                )
            if master_article.product_url and target_product_url != master_article.product_url:
                translated.content_html = translated.content_html.replace(
                    master_article.product_url, target_product_url
                )
            translated.product_url = target_product_url

            # Validate translation — non-blocking: log warning but publish anyway
            val = validate_content(
                translated.content_html, "article", site_cfg.language
            )
            if not val.passed:
                log.warning(
                    "article_translation_validation_warning",
                    site=site_cfg.slug,
                    issues=val.issues,
                    score=val.score,
                )
                # Non-blocking: continue with publish, record issues in log detail
            product_url = target_product_url

            # Save draft record to DB
            article_db = Article(
                topic_id=topic.id,
                site_id=site_db.id,
                language=site_cfg.language,
                title=translated.title,
                slug=translated.slug,
                content=translated.content_html,
                meta_title=translated.meta_title,
                meta_description=translated.meta_description,
                image_prompt=translated.image_prompt,
                image_url=image_url,
                status="pending_approval",
            )
            db.add(article_db)
            db.commit()
            db.refresh(article_db)

            # Publish as draft on WordPress
            publisher = WordPressPublisher(site_cfg)
            wp_result = publisher.publish_article(translated, image_url=image_url)

            article_db.wp_post_id = wp_result.post_id
            db.commit()

            val_note = f" | validation score={val.score}" if not val.passed else ""
            _log_publish(
                db, "article", article_db.id, site_db.id, "published",
                f"WP draft post_id={wp_result.post_id}{val_note}",
            )

            results.append({
                "site_slug": site_cfg.slug,
                "article_db_id": article_db.id,
                "post_id": wp_result.post_id,
                "post_url": wp_result.post_url,
            })
            log.info(
                "article_draft_published",
                site=site_cfg.slug,
                post_id=wp_result.post_id,
            )

            time.sleep(2)  # rate limit between sites

        except Exception as exc:
            log.error("site_article_job_failed", site=site_cfg.slug, error=str(exc))
            notify_error(f"Articolo {site_cfg.slug}", str(exc))
            try:
                _log_publish(db, "article", 0, site_db.id, "failed", str(exc))
            except Exception:
                pass  # site_db may not be defined if exception happened early

    return results


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


def article_job(site_slugs: Optional[list] = None) -> None:
    """
    Scheduled job: generate and publish one article per run as WP draft,
    translated for all sites that have WordPress configured.

    Args:
        site_slugs: optional list of site slugs to restrict publishing to.
                    If None or empty, all active WP sites are used.

    Flow:
      1. If no approved topic → send topic selection to Telegram and stop.
      2. Generate IT master article with content_agent.
      3. Validate with validator_agent.
      4. Generate featured image.
      5. Translate for each site with wp_api_url.
      6. Check product availability per site.
      7. Publish as WP draft per site.
      8. Notify Telegram with draft links + approve/reject buttons.
    """
    log.info("article_job_started", at=datetime.utcnow().isoformat())

    db = SessionLocal()
    try:
        topic = _pick_next_topic_for_article(db)

        if not topic:
            # No eligible topic — ask Omar to pick one
            pending = (
                db.query(ContentTopic)
                .filter(ContentTopic.status == "pending")
                .order_by(ContentTopic.priority.desc(), ContentTopic.created_at)
                .limit(8)
                .all()
            )
            notify_topic_selection(pending)
            log.info("article_job_no_eligible_topics_notified")
            return

        original_status = topic.status
        topic.status = "in_progress"
        db.commit()

        it_site = next(
            (s for s in get_all_active_sites() if s.slug == "herbago_it"), None
        )
        if not it_site:
            raise ValueError("herbago_it not found in active sites")

        settings = get_settings()
        max_attempts = settings.validator.get("max_regeneration_attempts", 3)

        # Use topic title as keyword; source_detail may refine it
        keyword = topic.title
        if topic.source_detail and "keyword:" in (topic.source_detail or ""):
            kw_part = topic.source_detail.split("keyword:")[-1].strip()
            if kw_part:
                keyword = kw_part

        try:
            # --- Generate IT master ---
            master = None
            for attempt in range(1, max_attempts + 1):
                try:
                    master = generate_article(
                        topic=topic.title,
                        keyword=keyword,
                        site_config=it_site,
                        product_url=topic.product_url or None,
                    )
                    break
                except Exception as exc:
                    log.warning(
                        "article_generation_failed", attempt=attempt, error=str(exc)
                    )
                    if attempt == max_attempts:
                        raise

            # --- Validate IT master ---
            val = validate_content(master.content_html, "article", it_site.language)
            if not val.passed:
                raise ValueError(
                    f"IT article validation failed: {'; '.join(val.issues)}"
                )

            # --- Generate image ---
            image_url: Optional[str] = None
            try:
                image_url = generate_image(master.image_prompt)
            except Exception as exc:
                log.warning("image_generation_failed", error=str(exc))
                # Non-fatal — publish without image

            # --- Publish to all sites ---
            draft_results = _process_article_for_sites(topic, image_url, master, db, site_slugs=site_slugs or None)

            if draft_results:
                notify_article_drafts_ready(topic.id, topic.title, draft_results)
                log.info(
                    "article_job_drafts_ready",
                    topic_id=topic.id,
                    sites=[r["site_slug"] for r in draft_results],
                )
            else:
                log.warning("article_job_no_drafts_published", topic_id=topic.id)

            # If emails were already done, both are done; otherwise mark article as done
            topic.status = "done" if original_status == "email_done" else "article_done"
            db.commit()
            log.info("article_job_finished", topic_id=topic.id, new_status=topic.status)

        except Exception as exc:
            topic.status = original_status  # reset for retry
            db.commit()
            log.error("article_job_failed", topic_id=topic.id, error=str(exc))
            notify_error("Article Job", str(exc))

    finally:
        db.close()


def keyword_research_job() -> None:
    """
    Monthly keyword research job: runs DataForSEO research for each site
    and saves snapshots to DB. Does not generate content.
    """
    from agents.seo_agent import research_keywords

    log.info("keyword_research_job_started", at=datetime.utcnow().isoformat())

    seed_keywords = [
        "herbalife colazione proteica",
        "integratori dimagrimento naturale",
        "shake sostituto pasto",
        "perdita peso sana",
        "energia sport nutrizione",
    ]

    db = SessionLocal()
    try:
        for site_cfg in get_all_active_sites():
            for seed in seed_keywords:
                try:
                    research_keywords(seed, site_cfg, db=db, limit=20, min_volume=100)
                    time.sleep(1)
                except Exception as exc:
                    log.warning(
                        "keyword_research_failed",
                        site=site_cfg.slug,
                        seed=seed,
                        error=str(exc),
                    )
    finally:
        db.close()

    log.info("keyword_research_job_complete")


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------


def start_scheduler() -> BackgroundScheduler:
    """
    Initialize and start the APScheduler background scheduler.

    Jobs:
      - email_job: every EMAIL_JOB_INTERVAL_DAYS days

    Returns the running scheduler (caller can shut it down with .shutdown()).
    """
    settings = get_settings()
    email_interval_days = settings.scheduler.get("email_job_interval_days", 15)
    article_interval_days = settings.scheduler.get("article_job_interval_days", 15)
    keyword_interval_days = settings.scheduler.get("keyword_research_interval_days", 30)

    scheduler = BackgroundScheduler()

    scheduler.add_job(
        func=email_job,
        trigger=IntervalTrigger(days=email_interval_days),
        id="email_job",
        name="Email pair generation and publication",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        func=article_job,
        trigger=IntervalTrigger(days=article_interval_days),
        id="article_job",
        name="Article generation and WP draft publication",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        func=keyword_research_job,
        trigger=IntervalTrigger(days=keyword_interval_days),
        id="keyword_research_job",
        name="DataForSEO keyword research snapshots",
        replace_existing=True,
        misfire_grace_time=7200,
    )

    scheduler.start()
    log.info(
        "scheduler_started",
        email_job_interval_days=email_interval_days,
        article_job_interval_days=article_interval_days,
        keyword_research_interval_days=keyword_interval_days,
    )

    return scheduler


if __name__ == "__main__":
    import time as _time
    log.info("starting_scheduler_standalone")
    sched = start_scheduler()
    try:
        while True:
            _time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()
        log.info("scheduler_stopped")
