"""
publishers/wp_importer.py

WordPress article importer for HerbaMarketer.

Fetches all published posts from a site's WordPress REST API and stores them
in the local `articles` table with source="wordpress_import".

Uses upsert logic: if a record with the same (wp_post_id, site_id) already
exists it is updated; otherwise a new row is inserted.

Public API:
    import_existing_articles(site_slug, db) -> ImportStats

Requires:
    SiteConfig.wp_api_url and WordPress credentials set in .env.
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
import structlog

from config import get_site_config
from core.database import Article, SessionLocal, Site

log = structlog.get_logger(__name__)

_PER_PAGE = 100
_REQUEST_TIMEOUT = 30
_DELAY_BETWEEN_PAGES = 0.5   # seconds — respect WP rate limits


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ImportStats:
    site_slug: str
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    total_fetched: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", " ", html or "")


def _count_words(html: str) -> int:
    """Count words in an HTML string by stripping tags first."""
    text = _strip_html(html)
    return len(text.split())


def _parse_wp_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse a WP ISO-8601 date string to datetime. Returns None on failure."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _fetch_posts_page(
    api_url: str,
    auth: tuple[str, str],
    page: int,
    statuses: str = "publish,draft,private",
) -> tuple[list[dict], int]:
    """
    Fetch one page of posts from the WP REST API.

    Returns (posts_list, total_pages).
    Raises httpx.HTTPStatusError on 4xx/5xx.
    """
    resp = httpx.get(
        f"{api_url}/posts",
        auth=auth,
        params={
            "per_page": _PER_PAGE,
            "page": page,
            "status": statuses,
            "_fields": (
                "id,title,slug,link,date,modified,status,"
                "content,excerpt,yoast_head_json"
            ),
        },
        timeout=_REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
    return resp.json(), total_pages


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def import_existing_articles(site_slug: str, db=None) -> ImportStats:
    """
    Fetch all posts from a site's WordPress REST API and upsert into articles.

    Args:
        site_slug: slug from sites.yaml (e.g. "herbago_it")
        db:        SQLAlchemy session. If None, creates and closes one internally.

    Returns:
        ImportStats with counts of inserted / updated / skipped / errors.
    """
    stats = ImportStats(site_slug=site_slug)
    close_db = db is None
    if db is None:
        db = SessionLocal()

    try:
        site_cfg = get_site_config(site_slug)

        if not site_cfg.wp_api_url:
            log.warning("wp_importer_no_api_url", site=site_slug)
            return stats

        if not site_cfg.wp_user or not site_cfg.wp_password:
            log.warning("wp_importer_no_credentials", site=site_slug)
            return stats

        api_url = site_cfg.wp_api_url.rstrip("/")
        auth = (site_cfg.wp_user, site_cfg.wp_password)

        # Ensure site row exists in DB
        site_db = db.query(Site).filter(Site.slug == site_slug).first()
        if not site_db:
            site_db = Site(
                slug=site_cfg.slug,
                url=site_cfg.url,
                language=site_cfg.language,
                locale=site_cfg.locale,
                mautic_campaign_id=site_cfg.mautic_campaign_id,
                email_prefix=site_cfg.email_prefix,
                platform=site_cfg.platform,
                active=site_cfg.active,
            )
            db.add(site_db)
            db.commit()
            db.refresh(site_db)

        log.info("wp_importer_start", site=site_slug, api_url=api_url)

        page = 1
        total_pages = 1

        while page <= total_pages:
            try:
                posts, total_pages = _fetch_posts_page(api_url, auth, page)
            except httpx.HTTPStatusError as exc:
                log.error(
                    "wp_importer_fetch_error",
                    site=site_slug,
                    page=page,
                    status=exc.response.status_code,
                )
                stats.errors += 1
                break
            except Exception as exc:
                log.error("wp_importer_fetch_exception", site=site_slug, page=page, error=str(exc))
                stats.errors += 1
                break

            stats.total_fetched += len(posts)

            for post in posts:
                try:
                    _upsert_post(post, site_db, site_cfg.language, db, stats)
                except Exception as exc:
                    log.error(
                        "wp_importer_upsert_error",
                        site=site_slug,
                        post_id=post.get("id"),
                        error=str(exc),
                    )
                    stats.errors += 1
                    db.rollback()

            page += 1
            if page <= total_pages:
                time.sleep(_DELAY_BETWEEN_PAGES)

        log.info(
            "wp_importer_complete",
            site=site_slug,
            total_fetched=stats.total_fetched,
            inserted=stats.inserted,
            updated=stats.updated,
            skipped=stats.skipped,
            errors=stats.errors,
        )

    finally:
        if close_db:
            db.close()

    return stats


def _upsert_post(
    post: dict,
    site_db: Site,
    language: str,
    db,
    stats: ImportStats,
) -> None:
    """Insert or update a single WP post in the articles table."""
    wp_post_id = int(post["id"])
    title = post.get("title", {}).get("rendered", "") or ""
    slug = post.get("slug", "") or ""
    link = post.get("link", "") or ""
    wp_status = post.get("status", "publish")
    content_html = post.get("content", {}).get("rendered", "") or ""
    excerpt_html = post.get("excerpt", {}).get("rendered", "") or ""
    published_at = _parse_wp_date(post.get("date"))

    # Yoast meta (optional)
    yoast = post.get("yoast_head_json") or {}
    meta_title = (yoast.get("title") or "")[:255] or None
    meta_description = (yoast.get("description") or "")[:500] or None

    word_count = _count_words(content_html) if content_html else None
    excerpt_text = _strip_html(excerpt_html).strip() or None

    # Map WP status to internal status
    internal_status = "published" if wp_status == "publish" else wp_status

    existing = (
        db.query(Article)
        .filter(
            Article.wp_post_id == wp_post_id,
            Article.site_id == site_db.id,
        )
        .first()
    )

    if existing:
        # Update only if WP modified date is newer or fields changed
        existing.title = title
        existing.slug = slug
        existing.wp_url = link
        existing.content = content_html
        existing.excerpt = excerpt_text
        existing.meta_title = meta_title
        existing.meta_description = meta_description
        existing.word_count = word_count
        existing.wp_published_at = published_at
        existing.status = internal_status
        existing.source = "wordpress_import"
        db.commit()
        stats.updated += 1
    else:
        article = Article(
            topic_id=None,
            site_id=site_db.id,
            language=language,
            title=title,
            slug=slug,
            content=content_html,
            excerpt=excerpt_text,
            meta_title=meta_title,
            meta_description=meta_description,
            wp_post_id=wp_post_id,
            wp_url=link,
            wp_published_at=published_at,
            word_count=word_count,
            source="wordpress_import",
            status=internal_status,
        )
        db.add(article)
        db.commit()
        stats.inserted += 1
