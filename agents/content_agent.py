"""
agents/content_agent.py

Generates email pairs (problem email + product email) via Claude API.

This agent is stateless: receives input, returns JSON output.
No database interaction — callers handle persistence.

Public API:
    generate_email_pair(topic, site_config) -> EmailPairOutput
    generate_article(topic, keyword, site_config) -> ArticleOutput
"""

import json
import os
from dataclasses import dataclass
from typing import Optional

import anthropic
import structlog

from config import SiteConfig

log = structlog.get_logger(__name__)

MODEL = "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class EmailContent:
    subject: str
    preheader: str
    body_html: str
    body_text: str


@dataclass
class EmailPairOutput:
    email_1: EmailContent   # problem/nurturing email
    email_2: EmailContent   # product/solution email
    language: str
    site_slug: str
    product_url: str = ""   # product URL used in email_2 CTA (for post-translation replacement)


@dataclass
class ArticleOutput:
    title: str
    slug: str
    content_html: str
    meta_title: str
    meta_description: str
    image_prompt: str
    language: str
    site_slug: str
    product_url: str = ""  # product URL used in CTA (for post-translation replacement)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _firstname_placeholder(site_config: SiteConfig) -> str:
    """Return the platform-specific first name personalization variable."""
    if site_config.platform == "brevo":
        return "{{ contact.NOME }}"
    return "{contactfield=firstname}"  # Mautic default


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_email_1_messages(topic: str, site_config: SiteConfig) -> list[dict]:
    """Build messages for Email 1 — the problem/nurturing email."""
    system_prompt = (
        f"Sei il marketing manager di {site_config.url} che vende prodotti Herbalife.\n"
        f"Il tuo mercato è {site_config.country}, lingua {site_config.language}. "
        f"Scrivi in modo professionale ma caldo, mai aggressivo commercialmente. "
        f"Non nominare mai concorrenti.\n"
        f"Non fare claim medici non verificabili (es. \"cura il diabete\").\n"
        f"Usa emoji con parsimonia."
    )

    pc_url = site_config.preferred_customer_url or ""
    dist_url = site_config.distributor_url or ""
    footer_html = (
        f"<hr style='margin: 30px 0; border: none; border-top: 1px solid #ddd;'>"
        f"<p style='font-size: 12px; color: #666;'>Vuoi acquistare i prodotti Herbalife?<br>"
        f"<a href='{pc_url}' style='color: #00A652;'>Diventa Cliente Privilegiato</a>"
        f" e approfitta di sconti fino al 38%</p>"
        f"<p style='font-size: 12px; color: #666;'>Interessato a diventare Distributore Indipendente?<br>"
        f"<a href='{dist_url}' style='color: #00A652;'>Scopri l'opportunità Herbalife</a></p>"
    )

    user_prompt = (
        f"Scrivi un'email di nurturing sul tema: {topic}\n\n"
        f"Requisiti:\n"
        f"- Oggetto: max 50 caratteri, curiosità o problema riconoscibile\n"
        f"- Preheader: max 80 caratteri\n"
        f"- Corpo: ESATTAMENTE 300-400 parole in {site_config.language} "
        f"(conta le parole: il minimo assoluto è 300, non scendere mai sotto)\n"
        f"- Prima riga dopo il saluto: 'Ciao {_firstname_placeholder(site_config)},' (variabile personalizzazione)\n"
        f"- Struttura: problema (2-3 paragrafi) → conseguenze (1-2 paragrafi) → "
        f"accenno soluzione (1 paragrafo) → chiusura che annuncia la prossima email\n"
        f"- NO link a articoli o pagine del sito — non inserire MAI URL inventati\n"
        f"- NO menzione prodotto specifico\n"
        f"- Footer ESATTO da usare alla fine del body_html (copia letteralmente):\n"
        f"{footer_html}\n\n"
        f"IMPORTANTE: body_html deve contenere almeno 300 parole di testo visibile. "
        f"Sviluppa ogni sezione con dettagli concreti, esempi pratici e tono empatico.\n\n"
        f"Rispondi ESCLUSIVAMENTE con JSON valido, nessun testo prima o dopo:\n"
        f'{{"subject": "...", "preheader": "...", "body_html": "...", "body_text": "..."}}'
    )

    return [
        {"role": "user", "content": user_prompt},
    ], system_prompt


def _build_email_2_messages(
    topic: str,
    product_name: str,
    product_url: str,
    site_config: SiteConfig,
) -> tuple[list[dict], str]:
    """Build messages for Email 2 — the product/solution email."""
    system_prompt = (
        f"Sei il marketing manager di {site_config.url} che vende prodotti Herbalife.\n"
        f"Il tuo mercato è {site_config.country}, lingua {site_config.language}. "
        f"Scrivi in modo professionale ma caldo, mai aggressivo commercialmente. "
        f"Non nominare mai concorrenti.\n"
        f"Non fare claim medici non verificabili (es. \"cura il diabete\").\n"
        f"Usa emoji con parsimonia."
    )

    pc_url = site_config.preferred_customer_url or ""
    dist_url = site_config.distributor_url or ""
    footer_html = (
        f"<hr style='margin: 30px 0; border: none; border-top: 1px solid #ddd;'>"
        f"<p style='font-size: 12px; color: #666;'>Vuoi acquistare i prodotti Herbalife?<br>"
        f"<a href='{pc_url}' style='color: #00A652;'>Diventa Cliente Privilegiato</a>"
        f" e approfitta di sconti fino al 38%</p>"
        f"<p style='font-size: 12px; color: #666;'>Interessato a diventare Distributore Indipendente?<br>"
        f"<a href='{dist_url}' style='color: #00A652;'>Scopri l'opportunità Herbalife</a></p>"
    )

    user_prompt = (
        f"Scrivi un'email che presenta il prodotto {product_name} "
        f"come soluzione al problema: {topic}\n\n"
        f"Requisiti:\n"
        f"- Oggetto: focus sul beneficio del prodotto, max 50 caratteri\n"
        f"- Preheader: max 80 caratteri\n"
        f"- Corpo: ESATTAMENTE 350-450 parole in {site_config.language} "
        f"(conta le parole: il minimo assoluto è 350, non scendere mai sotto)\n"
        f"- Prima riga dopo il saluto: 'Ciao {_firstname_placeholder(site_config)},' (variabile personalizzazione)\n"
        f"- Struttura: richiama problema (1 paragrafo) → presenta prodotto (1-2 paragrafi) → "
        f"benefici specifici con dettagli (2 paragrafi) → come usarlo (1 paragrafo) → "
        f"bottone CTA acquisto\n"
        f"- Bottone CTA: usa ESATTAMENTE questo URL verificato: {product_url}\n"
        f"  Non inventare altri URL. Se non hai un URL prodotto reale, NON mettere link.\n"
        f"- Footer ESATTO da usare alla fine del body_html (copia letteralmente):\n"
        f"{footer_html}\n\n"
        f"IMPORTANTE: body_html deve contenere almeno 350 parole di testo visibile. "
        f"Descrivi i benefici in modo concreto e specifico, usa esempi pratici.\n\n"
        f"Rispondi ESCLUSIVAMENTE con JSON valido, nessun testo prima o dopo:\n"
        f'{{"subject": "...", "preheader": "...", "body_html": "...", "body_text": "..."}}'
    )

    return [{"role": "user", "content": user_prompt}], system_prompt


def _build_article_messages(
    topic: str,
    keyword: str,
    site_config: SiteConfig,
    product_name: str = "Formula 1 Herbalife",
    product_url: str = "",
) -> tuple[list[dict], str]:
    """Build messages for article generation."""
    system_prompt = (
        f"Sei il content manager SEO di {site_config.url} che vende prodotti Herbalife.\n"
        f"Il tuo mercato è {site_config.country}, lingua {site_config.language}. "
        f"Scrivi in modo professionale ma accessibile. "
        f"Non fare claim medici non verificabili.\n"
        f"Usa emoji con parsimonia (max 3-4 nell'intero articolo)."
    )

    if product_url:
        cta_instruction = (
            f"- Blocco CTA finale: dopo \"In sintesi\", aggiungi un paragrafo che presenta "
            f"{product_name} come soluzione pratica, con un bottone/link HTML così:\n"
            f'  <a href="{product_url}" style="display:inline-block;background:#00A652;'
            f'color:#fff;padding:12px 28px;border-radius:6px;text-decoration:none;'
            f'font-weight:bold;">Scopri {product_name}</a>\n'
            f"  NON inventare altri URL — usa solo quello sopra."
        )
    else:
        cta_instruction = (
            f"- Blocco CTA finale: dopo \"In sintesi\", aggiungi un paragrafo che rimanda "
            f"ai prodotti Herbalife del sito {site_config.url} senza inventare URL specifici."
        )

    user_prompt = (
        f"Scrivi un articolo blog SEO in {site_config.language} su: {topic}\n\n"
        f"Requisiti:\n"
        f"- Lunghezza: 1600-1800 parole (MAI meno di 1500)\n"
        f"- Keyword primaria: {keyword} — usala nel titolo, primo paragrafo, 2-3 volte nel testo\n"
        f"- Struttura: intro problema → sviluppo approfondito → soluzioni generali → "
        f"prodotto Herbalife come soluzione → paragrafo \"In sintesi\" → CTA\n"
        f"- Tag: solo H3 e H4, mai H2 e mai H1\n"
        f"- Il penultimo blocco si chiama \"In sintesi\" o simile (MAI \"Conclusione\")\n"
        f"- Emoji: max 3-4 nell'intero articolo\n"
        f"- NO linee separatrici\n"
        f"{cta_instruction}\n"
        f"- Dopo articolo: meta_title (max 60 char) e meta_description (max 155 char)\n"
        f"- image_prompt: scena iper-realistica, benessere e natura, "
        f"NO prodotti, NO testo, NO persone riconoscibili\n\n"
        f"Rispondi ESCLUSIVAMENTE con JSON valido, nessun testo prima o dopo:\n"
        f'{{"title": "...", "slug": "...", "content_html": "...", '
        f'"meta_title": "...", "meta_description": "...", "image_prompt": "..."}}'
    )

    return [{"role": "user", "content": user_prompt}], system_prompt


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _call_claude(
    messages: list[dict],
    system_prompt: str,
    context: str = "",
    max_tokens: int = 4096,
) -> dict:
    """
    Call Claude API and parse JSON response.
    Raises ValueError if response is not valid JSON.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic(api_key=api_key)

    log.info("calling_claude", model=MODEL, context=context)

    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    )

    raw_text = response.content[0].text.strip()

    # Strip markdown code fences if Claude wraps the JSON
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        # Remove first and last fence lines
        raw_text = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.error("json_parse_error", raw=raw_text[:200], error=str(exc))
        raise ValueError(f"Claude returned invalid JSON: {exc}") from exc


def _parse_email_content(data: dict) -> EmailContent:
    """Parse and validate email JSON fields."""
    required = {"subject", "preheader", "body_html", "body_text"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Missing fields in email response: {missing}")

    return EmailContent(
        subject=data["subject"],
        preheader=data["preheader"],
        body_html=data["body_html"],
        body_text=data["body_text"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_email_pair(
    topic: str,
    site_config: SiteConfig,
    product_name: str = "Formula 1 Herbalife",
    product_url: Optional[str] = None,
) -> EmailPairOutput:
    """
    Generate an email pair (problem email + product email) for the given topic and site.

    Args:
        topic:        Content topic / theme for the email pair.
        site_config:  Site configuration (language, country, url, etc.).
        product_name: Herbalife product to feature in email 2.
        product_url:  Product page URL. If None, auto-looked up via sitemap.
                      If not found in sitemap either, email 2 is generated
                      without a product link rather than using an invented URL.

    Returns:
        EmailPairOutput with email_1 (problem) and email_2 (product).
    """
    if product_url is None:
        from core.sitemap import find_product_url
        product_url = find_product_url(product_name, site_config)
        if product_url:
            log.info("product_url_from_sitemap", url=product_url, product=product_name)
        else:
            log.warning(
                "product_url_not_found_in_sitemap",
                product=product_name,
                site=site_config.slug,
            )
            product_url = ""  # prompt instructs Claude not to invent URLs when empty

    log.info(
        "generating_email_pair",
        topic=topic,
        site=site_config.slug,
        language=site_config.language,
    )

    # --- Email 1: problem/nurturing ---
    messages_1, system_1 = _build_email_1_messages(topic, site_config)
    raw_1 = _call_claude(messages_1, system_1, context="email_1")
    email_1 = _parse_email_content(raw_1)

    # --- Email 2: product/solution ---
    messages_2, system_2 = _build_email_2_messages(
        topic, product_name, product_url, site_config
    )
    raw_2 = _call_claude(messages_2, system_2, context="email_2")
    email_2 = _parse_email_content(raw_2)

    log.info(
        "email_pair_generated",
        site=site_config.slug,
        email_1_subject=email_1.subject,
        email_2_subject=email_2.subject,
    )

    return EmailPairOutput(
        email_1=email_1,
        email_2=email_2,
        language=site_config.language,
        site_slug=site_config.slug,
        product_url=product_url,
    )


def generate_article(
    topic: str,
    keyword: str,
    site_config: SiteConfig,
    product_name: str = "Formula 1 Herbalife",
    product_url: Optional[str] = None,
) -> ArticleOutput:
    """
    Generate a SEO blog article for the given topic, keyword, and site.

    Args:
        topic:        Article topic / theme.
        keyword:      Primary SEO keyword.
        site_config:  Site configuration.
        product_name: Herbalife product to feature in the CTA.
        product_url:  Product page URL for the CTA button. If None, auto-looked
                      up via sitemap. If not found, CTA is generated without URL.

    Returns:
        ArticleOutput with title, slug, content_html, meta fields, image_prompt.
    """
    if product_url is None:
        from core.sitemap import find_product_url
        product_url = find_product_url(product_name, site_config) or ""
        if product_url:
            log.info("product_url_from_sitemap", url=product_url, product=product_name)
        else:
            log.warning("product_url_not_found_in_sitemap", product=product_name, site=site_config.slug)

    log.info(
        "generating_article",
        topic=topic,
        keyword=keyword,
        site=site_config.slug,
        language=site_config.language,
    )

    messages, system_prompt = _build_article_messages(
        topic, keyword, site_config, product_name, product_url
    )
    raw = _call_claude(messages, system_prompt, context="article", max_tokens=8192)

    required = {"title", "slug", "content_html", "meta_title", "meta_description", "image_prompt"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"Missing fields in article response: {missing}")

    article = ArticleOutput(
        title=raw["title"],
        slug=raw["slug"],
        content_html=raw["content_html"],
        meta_title=raw["meta_title"],
        meta_description=raw["meta_description"],
        image_prompt=raw["image_prompt"],
        language=site_config.language,
        site_slug=site_config.slug,
        product_url=product_url,
    )

    log.info("article_generated", site=site_config.slug, title=article.title)
    return article
