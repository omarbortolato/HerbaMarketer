"""
config/__init__.py

Loaders for sites.yaml and settings.yaml.
All config is read from YAML files; secrets come from .env.

Usage:
    from config import get_site_config, get_all_active_sites, settings

    site = get_site_config("herbago_it")
    # site.url, site.language, site.email_prefix, ...

    active_sites = get_all_active_sites()
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SiteConfig:
    slug: str
    url: str
    language: str
    locale: str
    platform: str                          # "mautic" | "brevo"
    active: bool = True
    mautic_campaign_id: Optional[int] = None
    email_prefix: Optional[str] = None
    brevo_list_id: Optional[int] = None
    wp_api_url: Optional[str] = None
    wp_user_env: Optional[str] = None
    wp_password_env: Optional[str] = None
    preferred_customer_url: Optional[str] = None
    distributor_url: Optional[str] = None
    wp_author_name: Optional[str] = None   # WP display name for posts (e.g. "Elena")
    ga4_property_id: Optional[str] = None  # GA4 property ID (None or "DA_AGGIUNGERE" = skip)

    @property
    def wp_user(self) -> Optional[str]:
        """Resolve WordPress username from environment variable."""
        if self.wp_user_env:
            return os.getenv(self.wp_user_env)
        return None

    @property
    def wp_password(self) -> Optional[str]:
        """Resolve WordPress application password from environment variable."""
        if self.wp_password_env:
            return os.getenv(self.wp_password_env)
        return None

    @property
    def country(self) -> str:
        """Derive country name from locale for use in prompts."""
        _locale_to_country = {
            "it-IT": "Italia",
            "fr-FR": "Francia",
            "de-DE": "Germania",
            "en-IE": "Irlanda",
            "en-GB": "Regno Unito",
            "en-US": "USA",
        }
        return _locale_to_country.get(self.locale, self.locale)


@dataclass
class GlobalSettings:
    scheduler: dict = field(default_factory=dict)
    content: dict = field(default_factory=dict)
    validator: dict = field(default_factory=dict)
    publishers: dict = field(default_factory=dict)
    logging: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_yaml(filename: str) -> dict:
    path = _CONFIG_DIR / filename
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_sites() -> dict[str, SiteConfig]:
    data = _load_yaml("sites.yaml")
    sites: dict[str, SiteConfig] = {}
    for slug, cfg in data.get("sites", {}).items():
        sites[slug] = SiteConfig(
            slug=slug,
            url=cfg["url"],
            language=cfg["language"],
            locale=cfg["locale"],
            platform=cfg.get("platform", "mautic"),
            active=cfg.get("active", True),
            mautic_campaign_id=cfg.get("mautic_campaign_id"),
            email_prefix=cfg.get("email_prefix"),
            brevo_list_id=cfg.get("brevo_list_id"),
            wp_api_url=cfg.get("wp_api_url"),
            wp_user_env=cfg.get("wp_user_env"),
            wp_password_env=cfg.get("wp_password_env"),
            preferred_customer_url=cfg.get("preferred_customer_url"),
            distributor_url=cfg.get("distributor_url"),
            wp_author_name=cfg.get("wp_author_name"),
            ga4_property_id=cfg.get("ga4_property_id"),
        )
    return sites


def _load_settings() -> GlobalSettings:
    data = _load_yaml("settings.yaml")
    return GlobalSettings(
        scheduler=data.get("scheduler", {}),
        content=data.get("content", {}),
        validator=data.get("validator", {}),
        publishers=data.get("publishers", {}),
        logging=data.get("logging", {}),
    )


# ---------------------------------------------------------------------------
# Module-level singletons (lazy-loaded)
# ---------------------------------------------------------------------------

_sites: Optional[dict[str, SiteConfig]] = None
_settings: Optional[GlobalSettings] = None


def _get_sites() -> dict[str, SiteConfig]:
    global _sites
    if _sites is None:
        _sites = _load_sites()
    return _sites


def get_site_config(slug: str) -> SiteConfig:
    """Return SiteConfig for the given slug. Raises KeyError if not found."""
    return _get_sites()[slug]


def get_all_active_sites() -> list[SiteConfig]:
    """Return all sites with active=true."""
    return [s for s in _get_sites().values() if s.active]


def get_settings() -> GlobalSettings:
    global _settings
    if _settings is None:
        _settings = _load_settings()
    return _settings


# Convenience alias
settings = get_settings()


# ---------------------------------------------------------------------------
# Config persistence (write-back to YAML)
# ---------------------------------------------------------------------------


def reset_config_cache() -> None:
    """Invalidate in-memory config caches so next read reloads from YAML."""
    global _sites, _settings
    _sites = None
    _settings = None


def save_site_field(slug: str, field: str, value) -> None:
    """
    Update a single field for one site in sites.yaml and reset the cache.
    Only fields in the allowed list can be changed.
    """
    _ALLOWED_SITE_FIELDS = {
        "mautic_campaign_id", "email_prefix", "brevo_list_id",
        "preferred_customer_url", "distributor_url", "active", "wp_author_name",
    }
    if field not in _ALLOWED_SITE_FIELDS:
        raise ValueError(f"Field '{field}' is not editable via the dashboard")

    path = _CONFIG_DIR / "sites.yaml"
    data = _load_yaml("sites.yaml")
    if slug not in data.get("sites", {}):
        raise KeyError(f"Site '{slug}' not found in sites.yaml")

    # Cast to correct type
    if field in ("mautic_campaign_id", "brevo_list_id"):
        value = int(value) if value else None
    elif field == "active":
        value = bool(value)

    data["sites"][slug][field] = value

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    reset_config_cache()


def add_site(
    slug: str,
    url: str,
    language: str,
    locale: str,
    platform: str,
    wp_api_url: Optional[str] = None,
    mautic_campaign_id: Optional[int] = None,
    email_prefix: Optional[str] = None,
    brevo_list_id: Optional[int] = None,
    preferred_customer_url: Optional[str] = None,
    distributor_url: Optional[str] = None,
    wp_author_name: Optional[str] = None,
) -> None:
    """
    Add a new site entry to sites.yaml and reset the cache.
    Auto-derives wp_user_env and wp_password_env from the slug.
    Raises ValueError if the slug already exists.
    """
    path = _CONFIG_DIR / "sites.yaml"
    data = _load_yaml("sites.yaml")

    if slug in data.get("sites", {}):
        raise ValueError(f"Site '{slug}' already exists in sites.yaml")

    env_prefix = slug.upper()
    entry: dict = {
        "url": url,
        "language": language,
        "locale": locale,
        "platform": platform,
        "active": True,
        "wp_user_env": f"WP_{env_prefix}_USER",
        "wp_password_env": f"WP_{env_prefix}_APP_PASSWORD",
    }
    if wp_api_url:
        entry["wp_api_url"] = wp_api_url
    if mautic_campaign_id:
        entry["mautic_campaign_id"] = int(mautic_campaign_id)
    if email_prefix:
        entry["email_prefix"] = email_prefix
    if brevo_list_id:
        entry["brevo_list_id"] = int(brevo_list_id)
    if preferred_customer_url:
        entry["preferred_customer_url"] = preferred_customer_url
    if distributor_url:
        entry["distributor_url"] = distributor_url
    if wp_author_name:
        entry["wp_author_name"] = wp_author_name

    if "sites" not in data:
        data["sites"] = {}
    data["sites"][slug] = entry

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    reset_config_cache()


def save_scheduler_settings(
    email_interval: int,
    article_interval: int,
    keyword_interval: int,
) -> None:
    """Update scheduler intervals in settings.yaml and reset the cache."""
    path = _CONFIG_DIR / "settings.yaml"
    data = _load_yaml("settings.yaml")
    data["scheduler"]["email_job_interval_days"] = int(email_interval)
    data["scheduler"]["article_job_interval_days"] = int(article_interval)
    data["scheduler"]["keyword_research_interval_days"] = int(keyword_interval)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    reset_config_cache()
