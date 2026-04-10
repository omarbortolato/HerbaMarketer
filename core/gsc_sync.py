"""
core/gsc_sync.py

Syncs Google Search Console data for all (or one) active sites.
Stores daily rows + top-queries/pages snapshots in the database.
"""

from datetime import date, timedelta
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from config import get_all_active_sites, get_site_config
from core.database import GscDailyRow, GscTopPage, GscTopQuery, SessionLocal, Site
from core.gsc_client import GSCClient

log = structlog.get_logger(__name__)

_PERIOD_DAYS = 30
_DAILY_BACKFILL_DAYS = 90


def sync_site_gsc(
    site_slug: str,
    db: Optional[Session] = None,
    backfill_days: int = _DAILY_BACKFILL_DAYS,
) -> dict:
    """
    Fetch GSC data for one site and upsert:
      - GscDailyRow  for each day in [today - backfill_days, yesterday]
      - GscTopQuery  snapshot (top 25 queries, last 30 days)
      - GscTopPage   snapshot (top 25 pages, last 30 days)

    Returns:
        {"success": True, "data": {...}}
        {"success": False, "error": "..."}
    """
    _own_session = db is None
    if _own_session:
        db = SessionLocal()

    try:
        site_config = get_site_config(site_slug)
    except KeyError:
        return {"success": False, "error": f"Site '{site_slug}' not found in config"}

    try:
        client = GSCClient(site_config.gsc_property)
        if not client.available:
            return {
                "success": False,
                "error": f"GSC client not available: {client.unavailable_reason}",
            }

        yesterday = date.today() - timedelta(days=1)
        from_date = yesterday - timedelta(days=backfill_days - 1)
        period_from = yesterday - timedelta(days=_PERIOD_DAYS - 1)

        daily_rows = client.get_daily_rows(from_date, yesterday)
        top_queries = client.get_top_queries(period_from, yesterday, limit=25)
        top_pages = client.get_top_pages(period_from, yesterday, limit=25)

        # Resolve site row
        site_row = db.query(Site).filter(Site.slug == site_slug).first()
        if site_row is None:
            site_row = Site(
                slug=site_config.slug,
                url=site_config.url,
                language=site_config.language,
                locale=site_config.locale,
                mautic_campaign_id=site_config.mautic_campaign_id,
                email_prefix=site_config.email_prefix,
                platform=site_config.platform,
                active=site_config.active,
            )
            db.add(site_row)
            db.flush()
        site_id = site_row.id
        today = date.today()

        # Upsert daily rows
        for item in daily_rows:
            row_date = (
                date.fromisoformat(item["date"])
                if isinstance(item["date"], str)
                else item["date"]
            )
            existing = (
                db.query(GscDailyRow)
                .filter(GscDailyRow.site_id == site_id, GscDailyRow.row_date == row_date)
                .first()
            )
            if existing is None:
                existing = GscDailyRow(site_id=site_id, row_date=row_date)
                db.add(existing)
            existing.clicks = item["clicks"]
            existing.impressions = item["impressions"]
            existing.ctr = item["ctr"]
            existing.position = item["position"]

        # Upsert top queries
        for item in top_queries:
            existing = (
                db.query(GscTopQuery)
                .filter(
                    GscTopQuery.site_id == site_id,
                    GscTopQuery.query == item["query"],
                    GscTopQuery.snapshot_date == today,
                )
                .first()
            )
            if existing is None:
                existing = GscTopQuery(
                    site_id=site_id,
                    query=item["query"],
                    snapshot_date=today,
                    period_days=_PERIOD_DAYS,
                )
                db.add(existing)
            existing.clicks = item["clicks"]
            existing.impressions = item["impressions"]
            existing.ctr = item["ctr"]
            existing.position = item["position"]

        # Upsert top pages
        for item in top_pages:
            existing = (
                db.query(GscTopPage)
                .filter(
                    GscTopPage.site_id == site_id,
                    GscTopPage.page == item["page"],
                    GscTopPage.snapshot_date == today,
                )
                .first()
            )
            if existing is None:
                existing = GscTopPage(
                    site_id=site_id,
                    page=item["page"],
                    snapshot_date=today,
                    period_days=_PERIOD_DAYS,
                )
                db.add(existing)
            existing.clicks = item["clicks"]
            existing.impressions = item["impressions"]
            existing.ctr = item["ctr"]
            existing.position = item["position"]

        db.commit()
        log.info(
            "gsc_sync.site_done",
            site=site_slug,
            daily_rows=len(daily_rows),
            queries=len(top_queries),
            pages=len(top_pages),
        )
        return {
            "success": True,
            "data": {
                "daily_rows": len(daily_rows),
                "top_queries": len(top_queries),
                "top_pages": len(top_pages),
            },
        }

    except Exception as exc:
        log.error("gsc_sync.site_error", site=site_slug, error=str(exc))
        if _own_session:
            db.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        if _own_session:
            db.close()


def sync_all_sites_gsc(backfill_days: int = 1) -> dict[str, dict]:
    """
    Sync GSC for every active site.
    backfill_days=1  → incremental daily (scheduled job)
    backfill_days=90 → full backfill (manual trigger from dashboard)
    """
    results: dict[str, dict] = {}
    db = SessionLocal()
    try:
        for site_cfg in get_all_active_sites():
            results[site_cfg.slug] = sync_site_gsc(
                site_cfg.slug, db=db, backfill_days=backfill_days
            )
    finally:
        db.close()
    successes = sum(1 for r in results.values() if r.get("success"))
    log.info("gsc_sync.all_done", total=len(results), success=successes)
    return results
