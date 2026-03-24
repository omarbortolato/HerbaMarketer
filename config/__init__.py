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
