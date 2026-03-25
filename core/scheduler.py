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
from core.sitemap import find_product_url
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


def _pick_next_topic(db) -> Optional[ContentTopic]:
    """
    Pick the next approved topic from the backlog.
    Priority: highest priority first, then oldest.
    """
    return (
        db.query(ContentTopic)
        .filter(ContentTopic.status == "approved")
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

    # Process all active sites (Mautic + Brevo)
    all_sites = get_all_active_sites()
    email_sites = [s for s in all_sites if s.platform in ("mautic", "brevo")]

    for site_cfg in email_sites:
        try:
            site_db = _get_or_create_site_db(db, site_cfg)
            if _already_published(db, topic.id, site_db.id):
                continue

            # Translate if needed
            translated = translate_email_pair(master_pair, site_cfg)

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


def email_job() -> None:
    """
    Main scheduled job: generate and publish one email pair per run,
    translated for all active Mautic sites.

    Triggered every EMAIL_JOB_INTERVAL_DAYS days.
    """
    log.info("email_job_started", at=datetime.utcnow().isoformat())

    db = SessionLocal()
    try:
        topic = _pick_next_topic(db)
        if not topic:
            log.info("email_job_no_approved_topics")
            notify_error("Email Job", "Nessun topic approvato in backlog — aggiungi topic con /addtopic")
            return

        # Mark as in_progress
        topic.status = "in_progress"
        db.commit()

        # IT is the master site
        it_site = next(
            (s for s in get_all_active_sites() if s.slug == "herbago_it"), None
        )
        if not it_site:
            raise ValueError("herbago_it not found in active sites — check sites.yaml")

        try:
            _process_site_email_with_translations(it_site, topic, db)
            topic.status = "done"
            db.commit()
            log.info("email_job_finished", topic_id=topic.id)
        except Exception as exc:
            topic.status = "approved"  # reset so it can be retried
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
) -> list[dict]:
    """
    Translate master article (IT), validate, check product availability,
    publish as WP draft per site. Returns list of {site_slug, post_id, post_url}.
    """
    settings = get_settings()
    all_sites = get_all_active_sites()
    wp_sites = [s for s in all_sites if s.wp_api_url]

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

            # Validate translation
            val = validate_content(
                translated.content_html, "article", site_cfg.language
            )
            if not val.passed:
                log.warning(
                    "article_translation_validation_failed",
                    site=site_cfg.slug,
                    issues=val.issues,
                )
                _log_publish(
                    db, "article", 0, site_db.id, "failed",
                    f"Validation failed: {'; '.join(val.issues)}",
                )
                notify_error(
                    f"Articolo {site_cfg.slug}",
                    f"Validazione traduzione fallita: {'; '.join(val.issues)}",
                )
                continue

            # Product availability check: look up product URL from sitemap.
            # Non-fatal: if not found, fall back to site root URL.
            product_url = find_product_url("Formula 1 Herbalife", site_cfg)
            if product_url is None:
                product_url = site_cfg.url
                log.warning(
                    "product_not_available_for_site_using_fallback",
                    site=site_cfg.slug,
                    product="Formula 1 Herbalife",
                    fallback=product_url,
                )

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

            _log_publish(
                db, "article", article_db.id, site_db.id, "published",
                f"WP draft post_id={wp_result.post_id}",
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

    return results


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


def article_job() -> None:
    """
    Scheduled job: generate and publish one article per run as WP draft,
    translated for all sites that have WordPress configured.

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
        topic = _pick_next_topic(db)

        if not topic:
            # No approved topic — ask Omar to pick one
            pending = (
                db.query(ContentTopic)
                .filter(ContentTopic.status == "pending")
                .order_by(ContentTopic.priority.desc(), ContentTopic.created_at)
                .limit(8)
                .all()
            )
            notify_topic_selection(pending)
            log.info("article_job_no_approved_topics_notified")
            return

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
            draft_results = _process_article_for_sites(topic, image_url, master, db)

            if draft_results:
                notify_article_drafts_ready(topic.id, topic.title, draft_results)
                log.info(
                    "article_job_drafts_ready",
                    topic_id=topic.id,
                    sites=[r["site_slug"] for r in draft_results],
                )
            else:
                log.warning("article_job_no_drafts_published", topic_id=topic.id)

            topic.status = "done"
            db.commit()

        except Exception as exc:
            topic.status = "approved"  # reset for retry
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
