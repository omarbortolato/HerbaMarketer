"""
agents/ads_advisor.py

Generates daily AI Google Ads suggestions for a site using the last 30 days
of Ads data stored in the database (AdsSnapshot + AdsDailyRow + AdsCampaignSnapshot).

Output: list of bullet-point strings (Italian), stored in AiSuggestion table.
"""

import json
from datetime import date, timedelta

import anthropic
import structlog

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """Sei un esperto di Google Ads per e-commerce nel settore benessere/integratori.
Analizza i dati delle campagne e genera suggerimenti pratici per ottimizzare le performance.
Rispondi ESCLUSIVAMENTE con un array JSON di stringhe. Massimo 5 suggerimenti. In italiano.
Ogni suggerimento deve citare numeri specifici e indicare un'azione concreta con priorità chiara.
Esempio valido: ["Il ROAS di 0.8x su herbago_de indica spesa non redditizia — considera di ridurre il budget del 30% o rivedere le keyword negative", "La campagna 'Brand IT' ha CTR 8% vs media account 2% — aumenta il budget di questa campagna del 20%"]
Non aggiungere testo fuori dall'array JSON."""


def generate_ads_suggestions(site_slug: str) -> list[str]:
    """
    Fetch Ads data from DB and call Claude to generate suggestions.
    Returns list of bullet strings, empty list on error.
    """
    from config import get_site_config
    from core.database import (
        AdsCampaignSnapshot,
        AdsDailyRow,
        AdsSnapshot,
        SessionLocal,
        Site,
    )

    db = SessionLocal()
    try:
        site_cfg = get_site_config(site_slug)
        site_db = db.query(Site).filter(Site.slug == site_slug).first()
        if not site_db:
            return []

        # Account-level snapshot (30 days)
        snap_30 = (
            db.query(AdsSnapshot)
            .filter(
                AdsSnapshot.site_id == site_db.id,
                AdsSnapshot.period_days == 30,
            )
            .order_by(AdsSnapshot.snapshot_date.desc())
            .first()
        )
        snap_7 = (
            db.query(AdsSnapshot)
            .filter(
                AdsSnapshot.site_id == site_db.id,
                AdsSnapshot.period_days == 7,
            )
            .order_by(AdsSnapshot.snapshot_date.desc())
            .first()
        )

        # Campaign snapshots (30 days, most recent)
        campaigns = []
        if snap_30:
            campaigns = (
                db.query(AdsCampaignSnapshot)
                .filter(
                    AdsCampaignSnapshot.site_id == site_db.id,
                    AdsCampaignSnapshot.period_days == 30,
                    AdsCampaignSnapshot.snapshot_date == snap_30.snapshot_date,
                )
                .order_by(AdsCampaignSnapshot.cost.desc())
                .all()
            )

        # Daily trend (last 30 days): sum per day
        thirty_ago = date.today() - timedelta(days=30)
        daily_rows = (
            db.query(AdsDailyRow)
            .filter(
                AdsDailyRow.site_id == site_db.id,
                AdsDailyRow.row_date >= thirty_ago,
            )
            .order_by(AdsDailyRow.row_date)
            .all()
        )

        if not snap_30 and not daily_rows:
            return []

        lines = [
            f"Sito: {site_slug} | URL: {site_cfg.url} | Customer ID: {site_cfg.google_ads_customer_id}",
        ]

        if snap_30:
            lines += [
                "",
                "=== Account — ultimi 30 giorni ===",
                f"Impressioni: {snap_30.impressions}",
                f"Click: {snap_30.clicks}",
                f"Costo: €{round(snap_30.cost or 0, 2)}",
                f"Conversioni: {snap_30.conversions}",
                f"Conv. Value: €{round(snap_30.conversions_value or 0, 2)}",
                f"ROAS: {snap_30.roas}x",
            ]

        if snap_7:
            lines += [
                "",
                "=== Account — ultimi 7 giorni ===",
                f"Costo: €{round(snap_7.cost or 0, 2)}",
                f"ROAS: {snap_7.roas}x",
                f"Conversioni: {snap_7.conversions}",
            ]

        if campaigns:
            lines.append("")
            lines.append("=== Campagne (30 giorni, ordinate per spesa) ===")
            for c in campaigns[:8]:
                lines.append(
                    f"  {c.campaign_name}: €{round(c.cost or 0, 2)} spesa, "
                    f"ROAS {c.roas}x, {c.clicks} click, {c.conversions} conv — status: {c.status}"
                )

        if daily_rows:
            # Aggregate by date for trend summary
            from collections import defaultdict
            daily_totals: dict = defaultdict(lambda: {"cost": 0.0, "conversions": 0.0})
            for r in daily_rows:
                ds = r.row_date.isoformat()
                daily_totals[ds]["cost"] += r.cost or 0.0
                daily_totals[ds]["conversions"] += r.conversions or 0.0

            costs = [round(v["cost"], 2) for v in daily_totals.values()]
            if costs:
                lines += [
                    "",
                    "=== Trend giornaliero ===",
                    f"Spesa media/giorno: €{round(sum(costs)/len(costs), 2)}",
                    f"Spesa max/giorno: €{max(costs)}",
                    f"Spesa min/giorno: €{min(costs)}",
                ]

        summary = "\n".join(lines)
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": summary}],
        )
        raw = response.content[0].text.strip()
        bullets = json.loads(raw)
        return bullets if isinstance(bullets, list) else []

    except Exception as exc:
        log.warning("ads_advisor.error", site=site_slug, error=str(exc))
        return []
    finally:
        db.close()


def save_ads_suggestions(site_slug: str) -> dict:
    """Generate and persist suggestions. Returns {"success", "count"}."""
    from core.database import AiSuggestion, SessionLocal, Site

    bullets = generate_ads_suggestions(site_slug)
    if not bullets:
        return {"success": False, "error": "no suggestions generated"}

    db = SessionLocal()
    try:
        site_db = db.query(Site).filter(Site.slug == site_slug).first()
        if not site_db:
            return {"success": False, "error": "site not in db"}

        today = date.today()
        existing = (
            db.query(AiSuggestion)
            .filter(
                AiSuggestion.site_id == site_db.id,
                AiSuggestion.type == "ads",
                AiSuggestion.suggestion_date == today,
            )
            .first()
        )
        if existing is None:
            existing = AiSuggestion(
                site_id=site_db.id, type="ads", suggestion_date=today
            )
            db.add(existing)
        existing.bullets = bullets
        db.commit()
        log.info("ads_advisor.saved", site=site_slug, count=len(bullets))
        return {"success": True, "count": len(bullets)}
    except Exception as exc:
        db.rollback()
        log.error("ads_advisor.save_error", site=site_slug, error=str(exc))
        return {"success": False, "error": str(exc)}
    finally:
        db.close()
