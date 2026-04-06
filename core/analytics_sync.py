"""
core/analytics_sync.py

Syncs GA4 data for all (or one) active sites and persists the results
as AnalyticsSnapshot rows in the database.

Usage:
    from core.analytics_sync import sync_all_sites_analytics, sync_site_analytics

    results = sync_all_sites_analytics()
    # results == {"herbago_it": {"success": True, "data": {...}}, ...}
"""

from datetime import date
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from config import get_all_active_sites, get_site_config
from core.database import AnalyticsSnapshot, SessionLocal, Site
from core.ga4_client import GA4Client

log = structlog.get_logger(__name__)

_PERIOD_DAYS = 30  # default snapshot window


def sync_site_analytics(
    site_slug: str,
    db: Optional[Session] = None,
    period_days: int = _PERIOD_DAYS,
) -> dict:
    """
    Fetch GA4 data for one site and upsert an AnalyticsSnapshot row.

    Returns:
        {"success": True, "data": {...}}   on success
        {"success": False, "error": "..."}  on failure
    """
    _own_session = db is None
    if _own_session:
        db = SessionLocal()

    try:
        site_config = get_site_config(site_slug)
    except KeyError:
        return {"success": False, "error": f"Site '{site_slug}' not found in config"}

    ga4_id = site_config.ga4_property_id
    if not ga4_id or ga4_id == "DA_AGGIUNGERE":
        log.debug("analytics_sync.skip_no_property", site=site_slug)
        return {"success": False, "error": "no ga4_property_id configured"}

    try:
        client = GA4Client(ga4_id)
        if not client.available:
            return {"success": False, "error": "GA4 client not available (check credentials)"}

        overview = client.get_site_overview(period_days)
        ecommerce = client.get_ecommerce_overview(period_days)
        returning_rate = client.get_returning_customers(period_days)
        traffic_sources = client.get_traffic_sources(period_days)

        # --- resolve (or create) DB site row ---
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
            log.info("analytics_sync.site_created_in_db", slug=site_slug)
        site_id = site_row.id

        today = date.today()

        # --- upsert ---
        snap = (
            db.query(AnalyticsSnapshot)
            .filter(
                AnalyticsSnapshot.site_id == site_id,
                AnalyticsSnapshot.snapshot_date == today,
                AnalyticsSnapshot.period_days == period_days,
            )
            .first()
        )
        if snap is None:
            snap = AnalyticsSnapshot(
                site_id=site_id,
                snapshot_date=today,
                period_days=period_days,
            )
            db.add(snap)

        if overview:
            snap.sessions = overview.get("sessions")
            snap.total_users = overview.get("total_users")
            snap.new_users = overview.get("new_users")
            snap.engagement_rate = overview.get("engagement_rate")
            snap.avg_session_duration = overview.get("avg_session_duration")
            snap.pageviews = overview.get("pageviews")
            snap.raw_overview = overview

        if ecommerce:
            snap.purchases = ecommerce.get("purchases")
            snap.revenue = ecommerce.get("revenue")
            snap.avg_order_value = ecommerce.get("avg_order_value")
            snap.add_to_carts = ecommerce.get("add_to_carts")
            snap.checkouts = ecommerce.get("checkouts")
            snap.cart_abandonment_rate = ecommerce.get("cart_abandonment_rate")
            snap.raw_ecommerce = ecommerce

        snap.returning_customer_rate = returning_rate
        snap.traffic_sources = traffic_sources

        db.commit()
        db.refresh(snap)

        data = {
            "sessions": snap.sessions,
            "total_users": snap.total_users,
            "new_users": snap.new_users,
            "pageviews": snap.pageviews,
            "purchases": snap.purchases,
            "revenue": snap.revenue,
            "avg_order_value": snap.avg_order_value,
            "cart_abandonment_rate": snap.cart_abandonment_rate,
            "returning_customer_rate": snap.returning_customer_rate,
            "snapshot_date": today.isoformat(),
        }
        log.info("analytics_sync.site_done", site=site_slug, sessions=snap.sessions)
        return {"success": True, "data": data}

    except Exception as exc:
        log.error("analytics_sync.site_error", site=site_slug, error=str(exc))
        if _own_session:
            db.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        if _own_session:
            db.close()


def sync_all_sites_analytics(
    period_days: int = _PERIOD_DAYS,
) -> dict[str, dict]:
    """
    Sync GA4 analytics for every active site that has a ga4_property_id.

    Returns a dict keyed by site slug:
        {
          "herbago_it": {"success": True, "data": {...}},
          "herbago_co_uk": {"success": False, "error": "no ga4_property_id configured"},
          ...
        }
    """
    results: dict[str, dict] = {}
    db = SessionLocal()
    try:
        for site_config in get_all_active_sites():
            results[site_config.slug] = sync_site_analytics(
                site_config.slug, db=db, period_days=period_days
            )
    finally:
        db.close()

    successes = sum(1 for r in results.values() if r.get("success"))
    log.info("analytics_sync.all_done", total=len(results), success=successes)
    return results
