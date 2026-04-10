"""
agents/analytics_advisor.py

Generates daily AI analytics suggestions for a site using the last 30 days
of GA4 + Google Search Console data stored in the database.

Output: list of bullet-point strings (Italian), stored in AiSuggestion table.
"""

import json
from datetime import date, timedelta
from typing import Optional

import anthropic
import structlog

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """Sei un esperto di analytics e SEO per e-commerce nel settore benessere/integratori.
Analizza i dati di performance del sito e genera suggerimenti pratici e specifici per migliorare le performance.
Rispondi ESCLUSIVAMENTE con un array JSON di stringhe. Massimo 5 suggerimenti. In italiano.
Ogni suggerimento deve citare numeri specifici dal contesto e indicare un'azione concreta.
Esempio valido: ["Il CTR organico del 1.2% è sotto la media del settore (2-3%) — ottimizza i meta title delle 3 pagine prodotto con più impressioni", "La posizione media di 18 per la query 'perdere peso naturalmente' indica potenziale SEO — crea un articolo dedicato con keyword nelle prime 3 posizioni"]
Non aggiungere testo fuori dall'array JSON."""


def generate_analytics_suggestions(site_slug: str) -> list[str]:
    """
    Fetch GA4 + GSC data from DB and call Claude to generate suggestions.
    Returns list of bullet strings, empty list on error.
    """
    from config import get_site_config
    from core.database import (
        AnalyticsSnapshot,
        GscDailyRow,
        GscTopPage,
        GscTopQuery,
        SessionLocal,
        Site,
    )

    db = SessionLocal()
    try:
        site_cfg = get_site_config(site_slug)
        site_db = db.query(Site).filter(Site.slug == site_slug).first()
        if not site_db:
            return []

        # GA4 snapshot (most recent 30-day)
        ga4 = (
            db.query(AnalyticsSnapshot)
            .filter(
                AnalyticsSnapshot.site_id == site_db.id,
                AnalyticsSnapshot.period_days == 30,
            )
            .order_by(AnalyticsSnapshot.snapshot_date.desc())
            .first()
        )

        # GSC daily (last 30 days)
        thirty_ago = date.today() - timedelta(days=30)
        gsc_days = (
            db.query(GscDailyRow)
            .filter(
                GscDailyRow.site_id == site_db.id,
                GscDailyRow.row_date >= thirty_ago,
            )
            .order_by(GscDailyRow.row_date)
            .all()
        )

        # GSC top queries (most recent snapshot)
        gsc_queries = (
            db.query(GscTopQuery)
            .filter(GscTopQuery.site_id == site_db.id)
            .order_by(GscTopQuery.snapshot_date.desc(), GscTopQuery.clicks.desc())
            .limit(10)
            .all()
        )

        # GSC top pages (most recent snapshot)
        gsc_pages = (
            db.query(GscTopPage)
            .filter(GscTopPage.site_id == site_db.id)
            .order_by(GscTopPage.snapshot_date.desc(), GscTopPage.clicks.desc())
            .limit(5)
            .all()
        )

        lines = [
            f"Sito: {site_slug} | URL: {site_cfg.url} | Lingua: {site_cfg.language}",
            "Periodo: ultimi 30 giorni",
        ]

        if ga4:
            lines += [
                "",
                "=== GA4 ===",
                f"Sessioni: {ga4.sessions}",
                f"Utenti totali: {ga4.total_users} (nuovi: {ga4.new_users})",
                f"Pageviews: {ga4.pageviews}",
                f"Engagement rate: {round((ga4.engagement_rate or 0)*100, 1)}%",
                f"Durata media sessione: {round((ga4.avg_session_duration or 0)/60, 1)} min",
                f"Revenue: €{round(ga4.revenue or 0, 2)}",
                f"Acquisti: {ga4.purchases}",
                f"AOV: €{round(ga4.avg_order_value or 0, 2)}",
            ]
            if ga4.cart_abandonment_rate is not None:
                lines.append(
                    f"Abbandono carrello: {round(ga4.cart_abandonment_rate * 100, 1)}%"
                )
            if ga4.returning_customer_rate is not None:
                lines.append(
                    f"Clienti di ritorno: {round(ga4.returning_customer_rate * 100, 1)}%"
                )
            if ga4.traffic_sources:
                sources_str = ", ".join(
                    f"{s.get('channel','?')}: {s.get('sessions','?')} sessioni"
                    for s in (ga4.traffic_sources or [])[:5]
                )
                lines.append(f"Fonti traffico: {sources_str}")
        else:
            lines.append("GA4: nessun dato disponibile")

        if gsc_days:
            total_clicks = sum(r.clicks or 0 for r in gsc_days)
            total_impr = sum(r.impressions or 0 for r in gsc_days)
            avg_ctr = round(total_clicks / total_impr * 100, 2) if total_impr > 0 else 0
            avg_pos = round(
                sum(r.position or 0 for r in gsc_days) / len(gsc_days), 1
            )
            lines += [
                "",
                "=== Google Search Console ===",
                f"Click organici totali: {total_clicks}",
                f"Impressioni totali: {total_impr}",
                f"CTR medio: {avg_ctr}%",
                f"Posizione media: {avg_pos}",
            ]

        if gsc_queries:
            lines.append("Top 10 query:")
            for q in gsc_queries:
                lines.append(
                    f'  "{q.query}": {q.clicks} click, {q.impressions} impr, '
                    f"CTR {q.ctr}%, pos {q.position}"
                )

        if gsc_pages:
            lines.append("Top 5 pagine:")
            for p in gsc_pages:
                lines.append(
                    f"  {p.page}: {p.clicks} click, pos {p.position}"
                )

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
        log.warning("analytics_advisor.error", site=site_slug, error=str(exc))
        return []
    finally:
        db.close()


def save_analytics_suggestions(site_slug: str) -> dict:
    """Generate and persist suggestions. Returns {"success", "count"}."""
    from core.database import AiSuggestion, SessionLocal, Site

    bullets = generate_analytics_suggestions(site_slug)
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
                AiSuggestion.type == "analytics",
                AiSuggestion.suggestion_date == today,
            )
            .first()
        )
        if existing is None:
            existing = AiSuggestion(
                site_id=site_db.id, type="analytics", suggestion_date=today
            )
            db.add(existing)
        existing.bullets = bullets
        db.commit()
        log.info("analytics_advisor.saved", site=site_slug, count=len(bullets))
        return {"success": True, "count": len(bullets)}
    except Exception as exc:
        db.rollback()
        log.error("analytics_advisor.save_error", site=site_slug, error=str(exc))
        return {"success": False, "error": str(exc)}
    finally:
        db.close()
