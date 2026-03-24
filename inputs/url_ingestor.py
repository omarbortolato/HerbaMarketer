"""
inputs/url_ingestor.py

Scrape a URL and extract a content topic via Claude.

Fetches the page, strips boilerplate HTML, passes the main text to
Claude for topic extraction, and saves the result as a ContentTopic.

Public API:
    ingest_url(url, db) -> ContentTopic | None
"""

import re
from typing import Optional

import httpx
import structlog
from bs4 import BeautifulSoup

from agents.content_agent import _call_claude
from core.database import ContentTopic

log = structlog.get_logger(__name__)

_MAX_TEXT_CHARS = 3000
_SCRAPE_TIMEOUT = 15
_USER_AGENT = "Mozilla/5.0 (compatible; HerbaMarketer/1.0)"


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------


def _scrape_text(url: str) -> str:
    """Fetch URL and extract the main readable text."""
    resp = httpx.get(
        url,
        timeout=_SCRAPE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove non-content tags
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "form", "noscript", "iframe"]):
        tag.decompose()

    # Prefer article/main content if present
    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find(id=re.compile(r"(content|article|main)", re.I))
        or soup.find("body")
    )
    text = (
        container.get_text(separator=" ", strip=True)
        if container
        else soup.get_text(separator=" ", strip=True)
    )

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_TEXT_CHARS]


# ---------------------------------------------------------------------------
# Claude topic extraction
# ---------------------------------------------------------------------------


def _extract_topic(text: str, source_url: str) -> dict:
    """
    Ask Claude to extract a blog topic and SEO keyword from scraped text.
    Returns dict with 'title' and 'keyword'.
    """
    messages = [
        {
            "role": "user",
            "content": (
                f"Analizza questo testo estratto da: {source_url}\n\n"
                f"Testo:\n{text}\n\n"
                f"Estrai UN argomento adatto a un articolo blog su "
                f"nutrizione, benessere o prodotti Herbalife, "
                f"ispirato da questo contenuto.\n\n"
                f"Rispondi SOLO con JSON valido:\n"
                f'{{\"title\": \"titolo argomento in italiano (max 80 char)\", '
                f'\"keyword\": \"keyword SEO principale in italiano (2-4 parole)\"}}'
            ),
        }
    ]
    system = (
        "Sei un esperto SEO che identifica argomenti di contenuto per un blog "
        "di nutrizione Herbalife. Rispondi sempre con JSON valido, nessun testo extra."
    )
    return _call_claude(messages, system, context="url_ingestor")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_url(url: str, db) -> Optional[ContentTopic]:
    """
    Scrape a URL, extract a content topic via Claude, and save to DB.

    Args:
        url: Full URL to scrape.
        db:  SQLAlchemy session.

    Returns:
        The created ContentTopic, or None if scraping/extraction fails.
    """
    log.info("url_ingest_start", url=url)

    try:
        text = _scrape_text(url)
    except Exception as exc:
        log.error("url_scrape_failed", url=url, error=str(exc))
        return None

    if not text.strip():
        log.warning("url_empty_content", url=url)
        return None

    try:
        result = _extract_topic(text, url)
    except Exception as exc:
        log.error("url_topic_extraction_failed", url=url, error=str(exc))
        return None

    title = result.get("title") or url
    keyword = result.get("keyword") or ""

    topic = ContentTopic(
        title=title,
        source="url_input",
        source_detail=f"{url} | keyword: {keyword}",
        status="pending",
        priority=5,
    )
    db.add(topic)
    db.commit()
    db.refresh(topic)

    log.info("url_topic_created", topic_id=topic.id, title=title, url=url)
    return topic
