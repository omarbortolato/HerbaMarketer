"""
agents/seo_agent.py

Keyword research via DataForSEO API.

Fetches related keywords and search volumes for a given seed keyword,
saves snapshots to DB, and proposes article topics.

Public API:
    research_keywords(seed_keyword, site_config, db) -> list[KeywordResult]
    propose_topics(seed_keyword, site_config, db, max_topics=2) -> list[str]
"""

import base64
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx
import structlog

from config import SiteConfig
from core.database import KeywordSnapshot, Site

log = structlog.get_logger(__name__)

_DATAFORSEO_URL = "https://api.dataforseo.com/v3"

_LANGUAGE_NAMES = {
    "it": "Italian",
    "fr": "French",
    "de": "German",
    "en": "English",
}

# DataForSEO location codes (Google Ads)
_LOCATION_CODES = {
    "it-IT": 2380,   # Italy
    "fr-FR": 2250,   # France
    "de-DE": 2276,   # Germany
    "en-IE": 2372,   # Ireland
    "en-GB": 2826,   # United Kingdom
    "en-US": 2840,   # United States
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class KeywordResult:
    keyword: str
    search_volume: int
    competition: float          # 0.0–1.0
    cpc: float                  # cost per click (USD)
    monthly_searches: list[int] = field(default_factory=list)  # last 12 months


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _get_auth_headers() -> dict:
    login = os.getenv("DATAFORSEO_LOGIN", "")
    password = os.getenv("DATAFORSEO_PASSWORD", "")
    if not login or not password:
        raise EnvironmentError(
            "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are not set"
        )
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Core API call
# ---------------------------------------------------------------------------


def _fetch_related_keywords(
    seed_keyword: str,
    location_code: int,
    language_name: str,
    limit: int,
    min_volume: int,
) -> list[KeywordResult]:
    """
    Call DataForSEO Labs related_keywords endpoint.
    Returns parsed KeywordResult list.
    """
    payload = [
        {
            "keyword": seed_keyword,
            "location_code": location_code,
            "language_name": language_name,
            "limit": limit,
            "filters": [
                ["keyword_data.keyword_info.search_volume", ">=", min_volume]
            ],
            "order_by": [
                ["keyword_data.keyword_info.search_volume", "desc"]
            ],
        }
    ]

    resp = httpx.post(
        f"{_DATAFORSEO_URL}/dataforseo_labs/google/related_keywords/live",
        headers=_get_auth_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    results: list[KeywordResult] = []
    for task in data.get("tasks", []):
        if task.get("status_code") != 20000:
            log.warning(
                "dataforseo_task_error",
                status=task.get("status_message"),
                seed=seed_keyword,
            )
            continue
        task_result = task.get("result") or []
        items = task_result[0].get("items", []) if task_result else []
        for item in items:
            kw_data = item.get("keyword_data", {})
            kw_info = kw_data.get("keyword_info", {})
            vol = kw_info.get("search_volume") or 0
            if vol < min_volume:
                continue
            monthly = [
                m.get("search_volume", 0)
                for m in (kw_info.get("monthly_searches") or [])
            ]
            results.append(
                KeywordResult(
                    keyword=kw_data.get("keyword", ""),
                    search_volume=vol,
                    competition=float(kw_info.get("competition") or 0.0),
                    cpc=float(kw_info.get("cpc") or 0.0),
                    monthly_searches=monthly,
                )
            )

    return sorted(results, key=lambda r: r.search_volume, reverse=True)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _save_keyword_snapshots(
    results: list[KeywordResult],
    site_db_id: int,
    db,
) -> None:
    """Persist keyword research results to keyword_snapshots table."""
    today = date.today()
    for kw in results:
        trend_score = (
            sum(kw.monthly_searches) / len(kw.monthly_searches)
            if kw.monthly_searches else 0.0
        )
        snapshot = KeywordSnapshot(
            site_id=site_db_id,
            keyword=kw.keyword,
            search_volume=kw.search_volume,
            difficulty=None,   # DataForSEO difficulty requires separate endpoint
            trend_score=trend_score,
            snapshot_date=today,
            raw_data={
                "competition": kw.competition,
                "cpc": kw.cpc,
                "monthly_searches": kw.monthly_searches,
            },
        )
        db.add(snapshot)
    db.commit()
    log.info("keyword_snapshots_saved", count=len(results), site_id=site_db_id)


def _get_or_create_site_id(db, site_cfg: SiteConfig) -> int:
    site = db.query(Site).filter(Site.slug == site_cfg.slug).first()
    if site:
        return site.id
    new_site = Site(
        slug=site_cfg.slug,
        url=site_cfg.url,
        language=site_cfg.language,
        locale=site_cfg.locale,
        mautic_campaign_id=site_cfg.mautic_campaign_id,
        email_prefix=site_cfg.email_prefix,
        platform=site_cfg.platform,
        active=site_cfg.active,
    )
    db.add(new_site)
    db.commit()
    db.refresh(new_site)
    return new_site.id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def research_keywords(
    seed_keyword: str,
    site_config: SiteConfig,
    db=None,
    limit: int = 30,
    min_volume: int = 100,
) -> list[KeywordResult]:
    """
    Fetch related keywords for a seed keyword on a given locale.

    Saves results as KeywordSnapshot rows if a db session is provided.

    Args:
        seed_keyword: The keyword to research (e.g. "herbalife colazione").
        site_config:  Site configuration (determines location + language).
        db:           Optional SQLAlchemy session for persisting snapshots.
        limit:        Max number of related keywords to fetch.
        min_volume:   Minimum monthly search volume to include.

    Returns:
        List of KeywordResult sorted by search_volume descending.
    """
    location_code = _LOCATION_CODES.get(site_config.locale, 2380)
    language_name = _LANGUAGE_NAMES.get(site_config.language, "Italian")

    log.info(
        "keyword_research_start",
        seed=seed_keyword,
        locale=site_config.locale,
        limit=limit,
    )

    results = _fetch_related_keywords(
        seed_keyword, location_code, language_name, limit, min_volume
    )

    log.info(
        "keyword_research_complete",
        seed=seed_keyword,
        site=site_config.slug,
        count=len(results),
    )

    if db and results:
        site_id = _get_or_create_site_id(db, site_config)
        _save_keyword_snapshots(results, site_id, db)

    return results


def propose_topics(
    seed_keyword: str,
    site_config: SiteConfig,
    db=None,
    max_topics: int = 2,
) -> list[str]:
    """
    Run keyword research and propose article topic titles.

    Topics are derived from the highest-volume related keywords.

    Args:
        seed_keyword: Seed keyword for research.
        site_config:  Site configuration.
        db:           Optional DB session for snapshot persistence.
        max_topics:   Max number of topic suggestions to return.

    Returns:
        List of topic title strings (capitalized keyword phrases).
    """
    keywords = research_keywords(
        seed_keyword,
        site_config,
        db=db,
        limit=50,
        min_volume=200,
    )

    if not keywords:
        log.warning("no_topics_proposed", seed=seed_keyword, site=site_config.slug)
        return []

    topics = [kw.keyword.strip().capitalize() for kw in keywords[:max_topics]]
    log.info(
        "topics_proposed",
        seed=seed_keyword,
        site=site_config.slug,
        topics=topics,
    )
    return topics
