"""
core/ads_sync.py

Syncs Google Ads data (account-level + campaigns) for all active sites
and persists them as AdsSnapshot / AdsCampaignSnapshot rows.
"""

from datetime import date, timedelta
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from config import get_all_active_sites, get_site_config
from core.database import AdsCampaignSnapshot, AdsDailyRow, AdsSnapshot, SessionLocal, Site
from core.google_ads_client import GoogleAdsClient

log = structlog.get_logger(__name__)

_PERIOD_DAYS = 30


def sync_site_ads(
    site_slug: str,
    db: Optional[Session] = None,
    period_days: int = _PERIOD_DAYS,
) -> dict:
    """
    Fetch Google Ads data for one site and upsert snapshots.

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

    customer_id = site_config.google_ads_customer_id
    if not customer_id:
        return {"success": False, "error": "no google_ads_customer_id configured"}

    try:
        client = GoogleAdsClient(customer_id)
        if not client.available:
            reason = client.unavailable_reason or "unknown"
            return {"success": False, "error": f"Google Ads client not available: {reason}"}

        overview = client.get_account_overview(period_days)
        campaigns = client.get_campaigns(period_days)

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
            log.info("ads_sync.site_created_in_db", slug=site_slug)
        site_id = site_row.id
        today = date.today()

        # --- Upsert account snapshot ---
        snap = (
            db.query(AdsSnapshot)
            .filter(
                AdsSnapshot.site_id == site_id,
                AdsSnapshot.snapshot_date == today,
                AdsSnapshot.period_days == period_days,
            )
            .first()
        )
        if snap is None:
            snap = AdsSnapshot(site_id=site_id, snapshot_date=today, period_days=period_days)
            db.add(snap)

        if overview:
            snap.impressions = overview.get("impressions")
            snap.clicks = overview.get("clicks")
            snap.ctr = overview.get("ctr")
            snap.cost = overview.get("cost")
            snap.conversions = overview.get("conversions")
            snap.conversions_value = overview.get("conversions_value")
            snap.roas = overview.get("roas")

        db.flush()

        # --- Upsert campaign snapshots ---
        for camp in campaigns:
            csnap = (
                db.query(AdsCampaignSnapshot)
                .filter(
                    AdsCampaignSnapshot.site_id == site_id,
                    AdsCampaignSnapshot.campaign_id == camp["campaign_id"],
                    AdsCampaignSnapshot.snapshot_date == today,
                    AdsCampaignSnapshot.period_days == period_days,
                )
                .first()
            )
            if csnap is None:
                csnap = AdsCampaignSnapshot(
                    site_id=site_id,
                    campaign_id=camp["campaign_id"],
                    snapshot_date=today,
                    period_days=period_days,
                )
                db.add(csnap)

            csnap.campaign_name = camp.get("campaign_name")
            csnap.status = camp.get("status")
            csnap.impressions = camp.get("impressions")
            csnap.clicks = camp.get("clicks")
            csnap.ctr = camp.get("ctr")
            csnap.cost = camp.get("cost")
            csnap.conversions = camp.get("conversions")
            csnap.conversions_value = camp.get("conversions_value")
            csnap.roas = camp.get("roas")

        db.commit()
        db.refresh(snap)

        data = {
            "impressions": snap.impressions,
            "clicks": snap.clicks,
            "cost": snap.cost,
            "conversions": snap.conversions,
            "roas": snap.roas,
            "campaigns_synced": len(campaigns),
            "snapshot_date": today.isoformat(),
        }
        log.info("ads_sync.site_done", site=site_slug, campaigns=len(campaigns), period=period_days)
        return {"success": True, "data": data}

    except Exception as exc:
        log.error("ads_sync.site_error", site=site_slug, error=str(exc))
        if _own_session:
            db.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        if _own_session:
            db.close()


def sync_site_ads_daily(
    site_slug: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    db: Optional[Session] = None,
) -> dict:
    """
    Fetch day-granular Google Ads data for one site and upsert AdsDailyRow records.

    Defaults: to_date=yesterday, from_date=90 days before to_date.
    For the scheduled daily job, pass from_date=yesterday, to_date=yesterday (1 row).
    For backfill from the dashboard, omit dates (fetches 90 days).
    """
    yesterday = date.today() - timedelta(days=1)
    if to_date is None:
        to_date = yesterday
    if from_date is None:
        from_date = to_date - timedelta(days=89)

    _own_session = db is None
    if _own_session:
        db = SessionLocal()

    try:
        site_config = get_site_config(site_slug)
    except KeyError:
        return {"success": False, "error": f"Site '{site_slug}' not found in config"}

    customer_id = site_config.google_ads_customer_id
    if not customer_id:
        return {"success": False, "error": "no google_ads_customer_id configured"}

    try:
        client = GoogleAdsClient(customer_id)
        if not client.available:
            return {"success": False, "error": f"Google Ads client not available: {client.unavailable_reason}"}

        daily_rows = client.get_daily_rows(from_date, to_date)

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

        for item in daily_rows:
            row_date = date.fromisoformat(item["row_date"]) if isinstance(item["row_date"], str) else item["row_date"]
            existing = (
                db.query(AdsDailyRow)
                .filter(
                    AdsDailyRow.site_id == site_id,
                    AdsDailyRow.campaign_id == item["campaign_id"],
                    AdsDailyRow.row_date == row_date,
                )
                .first()
            )
            if existing is None:
                existing = AdsDailyRow(
                    site_id=site_id,
                    campaign_id=item["campaign_id"],
                    row_date=row_date,
                )
                db.add(existing)
            existing.campaign_name = item["campaign_name"]
            existing.impressions = item["impressions"]
            existing.clicks = item["clicks"]
            existing.cost = item["cost"]
            existing.conversions = item["conversions"]
            existing.conversions_value = item["conversions_value"]

        db.commit()
        log.info("ads_sync_daily.site_done", site=site_slug, rows=len(daily_rows),
                 from_date=str(from_date), to_date=str(to_date))
        return {"success": True, "data": {"rows_synced": len(daily_rows),
                                          "from_date": str(from_date), "to_date": str(to_date)}}

    except Exception as exc:
        log.error("ads_sync_daily.site_error", site=site_slug, error=str(exc))
        if _own_session:
            db.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        if _own_session:
            db.close()


def sync_all_sites_ads_daily(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict[str, dict]:
    """Sync day-granular Google Ads data for every active site with a customer_id."""
    results: dict[str, dict] = {}
    db = SessionLocal()
    try:
        for site_cfg in get_all_active_sites():
            results[site_cfg.slug] = sync_site_ads_daily(
                site_cfg.slug, from_date=from_date, to_date=to_date, db=db
            )
    finally:
        db.close()
    successes = sum(1 for r in results.values() if r.get("success"))
    log.info("ads_sync_daily.all_done", total=len(results), success=successes)
    return results


def sync_all_sites_ads(period_days: int = _PERIOD_DAYS) -> dict[str, dict]:
    """
    Sync Google Ads for every active site with a google_ads_customer_id.

    Returns a dict keyed by site slug.
    """
    results: dict[str, dict] = {}
    db = SessionLocal()
    try:
        for site_cfg in get_all_active_sites():
            results[site_cfg.slug] = sync_site_ads(site_cfg.slug, db=db, period_days=period_days)
    finally:
        db.close()

    successes = sum(1 for r in results.values() if r.get("success"))
    log.info("ads_sync.all_done", total=len(results), success=successes)
    return results
