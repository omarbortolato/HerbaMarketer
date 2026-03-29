"""
agents/translator_agent.py

Translates generated content (email pairs or articles) from Italian
into the target language of each active site.

This agent is stateless: receives source content + target language,
returns translated content in the same structure.

Public API:
    translate_email_pair(email_pair, target_site_config) -> EmailPairOutput
    translate_article(article, target_site_config)       -> ArticleOutput
"""

import json

import structlog

from agents.content_agent import ArticleOutput, EmailContent, EmailPairOutput, _call_claude
from config import SiteConfig

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_email_translation_messages(
    email: EmailContent,
    source_language: str,
    target_language: str,
    target_country: str,
    email_role: str,  # "problema" | "prodotto"
) -> tuple[list[dict], str]:
    system_prompt = (
        f"Sei un traduttore professionale specializzato in marketing per prodotti Herbalife. "
        f"Traduci dall'{source_language} in {target_language} per il mercato {target_country}. "
        f"Mantieni il tono professionale ma caldo, adatta le espressioni idiomatiche "
        f"alla cultura locale. Non tradurre URL, nomi di prodotti Herbalife, o codici. "
        f"Non fare claim medici non verificabili."
    )

    source_json = json.dumps(
        {
            "subject": email.subject,
            "preheader": email.preheader,
            "body_html": email.body_html,
            "body_text": email.body_text,
        },
        ensure_ascii=False,
    )

    user_prompt = (
        f"Traduci questa email di marketing ({email_role}) in {target_language} "
        f"per il mercato {target_country}.\n\n"
        f"Regole:\n"
        f"- Adatta culturalmente (non tradurre letteralmente)\n"
        f"- Mantieni la stessa struttura HTML\n"
        f"- subject max 50 caratteri, preheader max 80 caratteri\n\n"
        f"Input:\n{source_json}\n\n"
        f"Rispondi ESCLUSIVAMENTE con JSON valido:\n"
        f'{{"subject": "...", "preheader": "...", "body_html": "...", "body_text": "..."}}'
    )

    return [{"role": "user", "content": user_prompt}], system_prompt


def _build_article_translation_messages(
    article: ArticleOutput,
    source_language: str,
    target_language: str,
    target_country: str,
) -> tuple[list[dict], str]:
    system_prompt = (
        f"Sei un traduttore professionale SEO specializzato in contenuti Herbalife. "
        f"Traduci dall'{source_language} in {target_language} per il mercato {target_country}. "
        f"Mantieni i tag HTML, adatta le keyword SEO alla lingua target, "
        f"preserva la struttura H3/H4. Non fare claim medici non verificabili. "
        f"Adatta culturalmente, non tradurre letteralmente."
    )

    source_json = json.dumps(
        {
            "title": article.title,
            "slug": article.slug,
            "content_html": article.content_html,
            "meta_title": article.meta_title,
            "meta_description": article.meta_description,
            "image_prompt": article.image_prompt,
        },
        ensure_ascii=False,
    )

    user_prompt = (
        f"Traduci questo articolo blog SEO in {target_language} per il mercato {target_country}.\n\n"
        f"Regole:\n"
        f"- Mantieni struttura HTML (H3, H4, paragrafi)\n"
        f"- Adatta title e slug alla lingua target\n"
        f"- meta_title max 60 caratteri, meta_description max 155 caratteri\n"
        f"- Adatta image_prompt se contiene riferimenti culturali specifici\n"
        f"- NON tradurre nomi prodotti Herbalife\n\n"
        f"Input:\n{source_json}\n\n"
        f"Rispondi ESCLUSIVAMENTE con JSON valido:\n"
        f'{{"title": "...", "slug": "...", "content_html": "...", '
        f'"meta_title": "...", "meta_description": "...", "image_prompt": "..."}}'
    )

    return [{"role": "user", "content": user_prompt}], system_prompt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def translate_email_pair(
    email_pair: EmailPairOutput,
    target_site: SiteConfig,
) -> EmailPairOutput:
    """
    Translate an email pair from Italian into the target site's language.

    If source and target language are the same, returns the original content
    with the target site slug (no API call needed).

    Args:
        email_pair:  Source EmailPairOutput (Italian master).
        target_site: Destination site config.

    Returns:
        New EmailPairOutput with translated content.
    """
    target_lang = target_site.language

    # Same language: no translation needed, just re-tag with target site
    if email_pair.language == target_lang:
        log.info(
            "translation_skipped_same_language",
            source_site=email_pair.site_slug,
            target_site=target_site.slug,
            language=target_lang,
        )
        return EmailPairOutput(
            email_1=email_pair.email_1,
            email_2=email_pair.email_2,
            language=target_lang,
            site_slug=target_site.slug,
        )

    log.info(
        "translating_email_pair",
        source_lang=email_pair.language,
        target_lang=target_lang,
        target_site=target_site.slug,
    )

    msgs_1, sys_1 = _build_email_translation_messages(
        email_pair.email_1,
        source_language="italiano",
        target_language=target_lang,
        target_country=target_site.country,
        email_role="problema",
    )
    raw_1 = _call_claude(msgs_1, sys_1, context=f"translate_email_1→{target_lang}")
    email_1 = EmailContent(
        subject=raw_1["subject"],
        preheader=raw_1["preheader"],
        body_html=raw_1["body_html"],
        body_text=raw_1["body_text"],
    )

    msgs_2, sys_2 = _build_email_translation_messages(
        email_pair.email_2,
        source_language="italiano",
        target_language=target_lang,
        target_country=target_site.country,
        email_role="prodotto",
    )
    raw_2 = _call_claude(msgs_2, sys_2, context=f"translate_email_2→{target_lang}")
    email_2 = EmailContent(
        subject=raw_2["subject"],
        preheader=raw_2["preheader"],
        body_html=raw_2["body_html"],
        body_text=raw_2["body_text"],
    )

    log.info(
        "email_pair_translated",
        target_site=target_site.slug,
        email_1_subject=email_1.subject,
        email_2_subject=email_2.subject,
    )

    return EmailPairOutput(
        email_1=email_1,
        email_2=email_2,
        language=target_lang,
        site_slug=target_site.slug,
    )


def translate_article(
    article: ArticleOutput,
    target_site: SiteConfig,
) -> ArticleOutput:
    """
    Translate an article from Italian into the target site's language.

    If source and target language are the same, returns the original
    content re-tagged with the target site (no API call).

    Args:
        article:     Source ArticleOutput (Italian master).
        target_site: Destination site config.

    Returns:
        New ArticleOutput with translated content.
    """
    target_lang = target_site.language

    if article.language == target_lang:
        log.info(
            "translation_skipped_same_language",
            source_site=article.site_slug,
            target_site=target_site.slug,
            language=target_lang,
        )
        return ArticleOutput(
            title=article.title,
            slug=article.slug,
            content_html=article.content_html,
            meta_title=article.meta_title,
            meta_description=article.meta_description,
            image_prompt=article.image_prompt,
            language=target_lang,
            site_slug=target_site.slug,
        )

    log.info(
        "translating_article",
        source_lang=article.language,
        target_lang=target_lang,
        target_site=target_site.slug,
    )

    msgs, sys_prompt = _build_article_translation_messages(
        article,
        source_language="italiano",
        target_language=target_lang,
        target_country=target_site.country,
    )
    raw = _call_claude(msgs, sys_prompt, context=f"translate_article→{target_lang}", max_tokens=8192)

    required = {"title", "slug", "content_html", "meta_title", "meta_description", "image_prompt"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"Missing fields in article translation: {missing}")

    translated = ArticleOutput(
        title=raw["title"],
        slug=raw["slug"],
        content_html=raw["content_html"],
        meta_title=raw["meta_title"],
        meta_description=raw["meta_description"],
        image_prompt=raw["image_prompt"],
        language=target_lang,
        site_slug=target_site.slug,
    )

    log.info("article_translated", target_site=target_site.slug, title=translated.title)
    return translated
