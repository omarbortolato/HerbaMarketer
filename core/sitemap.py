"""
core/sitemap.py

Product URL lookup via sitemap.

Fetches the product sitemap for a site and finds the best matching
URL for a given product name using fuzzy slug matching.

Convention: product sitemaps are at {site_url}/product-sitemap.xml
Falls back to sitemap_index.xml to discover the correct URL.

Public API:
    find_product_url(product_name, site_config) -> str | None
    get_product_urls(site_config) -> list[str]
"""

import re
from functools import lru_cache
from typing import Optional

import httpx
import structlog

from config import SiteConfig

log = structlog.get_logger(__name__)

_SITEMAP_CANDIDATES = [
    "{url}/product-sitemap.xml",
    "{url}/sitemap_index.xml",
    "{url}/sitemap-products.xml",
    "{url}/wp-sitemap-posts-product-1.xml",
]


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


def _fetch_urls_from_xml(xml: str) -> list[str]:
    """Extract all <loc> URLs from a sitemap XML string."""
    return re.findall(r"<loc>\s*(https?://[^\s<]+)\s*</loc>", xml)


def _fetch_sitemap_index_product_url(base_url: str, xml: str) -> Optional[str]:
    """
    In a sitemap index, find the URL of the product sub-sitemap.
    Returns the URL to fetch next, or None.
    """
    locs = _fetch_urls_from_xml(xml)
    for loc in locs:
        if "product" in loc.lower():
            return loc
    return None


@lru_cache(maxsize=16)
def get_product_urls(site_url: str) -> list[str]:
    """
    Fetch and cache all product URLs for a site.
    Tries multiple sitemap locations in order.

    Args:
        site_url: Base URL of the site (e.g. 'https://herbago.it')

    Returns:
        List of product page URLs. Empty list if sitemap not found.
    """
    url = site_url.rstrip("/")

    for template in _SITEMAP_CANDIDATES:
        sitemap_url = template.format(url=url)
        try:
            resp = httpx.get(sitemap_url, timeout=15, follow_redirects=True)
            if resp.status_code != 200:
                continue

            xml = resp.text

            # If it's a sitemap index, drill into product sub-sitemap
            if "<sitemapindex" in xml:
                product_sitemap_url = _fetch_sitemap_index_product_url(url, xml)
                if product_sitemap_url:
                    resp2 = httpx.get(product_sitemap_url, timeout=15, follow_redirects=True)
                    if resp2.status_code == 200:
                        urls = _fetch_urls_from_xml(resp2.text)
                        if urls:
                            log.info(
                                "sitemap_loaded",
                                site=url,
                                source=product_sitemap_url,
                                count=len(urls),
                            )
                            return urls
                continue

            # Direct product sitemap
            urls = _fetch_urls_from_xml(xml)
            if urls:
                log.info("sitemap_loaded", site=url, source=sitemap_url, count=len(urls))
                return urls

        except httpx.RequestError as exc:
            log.debug("sitemap_fetch_error", url=sitemap_url, error=str(exc))
            continue

    log.warning("sitemap_not_found", site=url)
    return []


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------


def _to_slug_tokens(text: str) -> set[str]:
    """
    Normalize a product name or URL slug into a set of tokens for matching.
    Removes common stop words that add noise.
    """
    stop_words = {
        "herbalife", "del", "della", "delle", "di", "da", "per", "con",
        "il", "la", "lo", "le", "i", "gli", "un", "una",
        "the", "of", "for", "with", "and", "in",
        "de", "la", "le", "les", "des", "du", "et",
        "ml", "kg", "pz",
    }
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    # Keep single digits (e.g. "1" in "formula 1") and short tokens not in stopwords
    return {t for t in tokens if t not in stop_words and len(t) >= 1}


def _extract_product_slug(url: str) -> str:
    """
    Extract just the product slug from a URL for scoring.
    e.g. 'https://herbago.it/p/formula-1-vaniglia-550-g/' → 'formula-1-vaniglia-550-g'
    Falls back to the full URL if no /p/ path segment found.
    """
    match = re.search(r"/p/([^/]+)", url)
    return match.group(1) if match else url


def _score_url(product_tokens: set[str], url: str, product_name: str = "") -> int:
    """
    Score a product URL against a set of product name tokens.
    Operates on the product slug only (not the full URL) to avoid noise.
    Higher = better match.

    Bonus: +15 if the slug starts with the first significant word of the product name,
    ensuring e.g. "Formula 1" matches formula-1-vaniglia before cucchiaio-per-formula-1.
    """
    slug = _extract_product_slug(url)
    url_tokens = _to_slug_tokens(slug)
    if not product_tokens or not url_tokens:
        return 0
    intersection = product_tokens & url_tokens
    score = len(intersection) * 10 - len(url_tokens - product_tokens)

    # Positional bonus: first product token starts the slug
    if product_name:
        first_token = re.findall(r"[a-z0-9]+", product_name.lower())
        stop_words = {"herbalife", "the", "le", "la", "de"}
        first_token = next((t for t in first_token if t not in stop_words), None)
        if first_token and slug.lower().startswith(first_token):
            score += 15

    return score


def find_product_url(
    product_name: str,
    site_config: SiteConfig,
    min_score: int = 5,
) -> Optional[str]:
    """
    Find the best matching product URL for a given product name on a site.

    Args:
        product_name: Human-readable product name (e.g. "Formula 1 Herbalife")
        site_config:  Site configuration.
        min_score:    Minimum match score to accept a result (default 5).

    Returns:
        Best matching product URL, or None if no good match found.
    """
    urls = get_product_urls(site_config.url)
    if not urls:
        log.warning("no_product_urls_found", site=site_config.slug)
        return None

    product_tokens = _to_slug_tokens(product_name)
    if not product_tokens:
        return None

    scored = [(url, _score_url(product_tokens, url, product_name)) for url in urls]
    scored.sort(key=lambda x: x[1], reverse=True)

    best_url, best_score = scored[0]

    if best_score < min_score:
        log.warning(
            "no_good_product_url_match",
            product=product_name,
            site=site_config.slug,
            best_score=best_score,
            best_url=best_url,
        )
        return None

    log.info(
        "product_url_found",
        product=product_name,
        site=site_config.slug,
        url=best_url,
        score=best_score,
    )
    return best_url
