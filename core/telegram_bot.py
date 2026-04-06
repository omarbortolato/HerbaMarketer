"""
core/telegram_bot.py

Telegram bot for HerbaMarketer human-in-the-loop supervision.

Handles:
- Outbound notifications (email pair preview, publish confirmation, errors)
- Inbound commands: /status /topics /addtopic /approve /preview /publish /sites /report
- Inline keyboard buttons for approve/reject on email pairs

The bot runs as a long-polling process alongside the scheduler.

Usage (standalone):
    python -m core.telegram_bot

Usage (from scheduler):
    from core.telegram_bot import notify_email_pair_ready, notify_publish_result
"""

import asyncio
import os
import textwrap
from typing import Optional

import structlog
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import get_all_active_sites, get_site_config
from core.database import Article, SessionLocal, ContentTopic, EmailPair, PublishLog, Site

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID_OMAR = os.getenv("TELEGRAM_CHAT_ID_OMAR", "")
CHAT_ID_EMILIANO = os.getenv("TELEGRAM_CHAT_ID_EMILIANO", "")  # optional


def _get_notify_chat_ids() -> list[str]:
    """Return list of chat IDs to notify (Omar always, Emiliano if set)."""
    ids = [CHAT_ID_OMAR] if CHAT_ID_OMAR else []
    if CHAT_ID_EMILIANO:
        ids.append(CHAT_ID_EMILIANO)
    return ids


# ---------------------------------------------------------------------------
# Outbound notifications (async helpers)
# ---------------------------------------------------------------------------


async def _send_message(text: str, chat_id: str, reply_markup=None) -> None:
    """Send a message to a specific chat ID."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    async with bot:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


def notify_email_pair_ready(
    email_pair_id: int,
    site_slug: str,
    topic_title: str,
    email_1_subject: str,
    email_2_subject: str,
) -> None:
    """
    Send a Telegram notification with email pair preview and approve/reject buttons.
    Called by the scheduler after content generation + validation.
    """
    text = (
        f"📧 <b>Nuova coppia email pronta</b>\n\n"
        f"🌐 <b>Sito:</b> {site_slug}\n"
        f"📌 <b>Argomento:</b> {textwrap.shorten(topic_title, width=80)}\n\n"
        f"<b>Email 1 (problema):</b>\n"
        f"  Oggetto: <i>{email_1_subject}</i>\n\n"
        f"<b>Email 2 (prodotto):</b>\n"
        f"  Oggetto: <i>{email_2_subject}</i>\n\n"
        f"Usa i bottoni per approvare o rifiutare."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Approva e pubblica",
                callback_data=f"approve_email:{email_pair_id}",
            ),
            InlineKeyboardButton(
                "❌ Rifiuta",
                callback_data=f"reject_email:{email_pair_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "👁 Anteprima completa",
                callback_data=f"preview_email:{email_pair_id}",
            ),
        ],
    ])

    for chat_id in _get_notify_chat_ids():
        asyncio.run(_send_message(text, chat_id, reply_markup=keyboard))
        log.info("telegram_notification_sent", type="email_pair_ready", chat_id=chat_id)


def notify_publish_result(
    site_slug: str,
    email_1_name: str,
    email_2_name: str,
    success: bool,
    error: Optional[str] = None,
) -> None:
    """Notify the result of a Mautic publish operation."""
    if success:
        text = (
            f"✅ <b>Email pubblicate su Mautic</b>\n\n"
            f"🌐 Sito: {site_slug}\n"
            f"📨 {email_1_name}\n"
            f"📨 {email_2_name}\n\n"
            f"Aggiunte in coda alla campagna (+14gg)"
        )
    else:
        text = (
            f"❌ <b>Errore pubblicazione Mautic</b>\n\n"
            f"🌐 Sito: {site_slug}\n"
            f"Errore: <code>{error or 'sconosciuto'}</code>"
        )

    for chat_id in _get_notify_chat_ids():
        asyncio.run(_send_message(text, chat_id))
        log.info("telegram_notification_sent", type="publish_result", success=success)


def notify_brevo_templates_ready(
    topic_title: str,
    template_1_name: str,
    template_1_id: int,
    template_2_name: str,
    template_2_id: int,
) -> None:
    """
    Notify Omar that two Brevo templates are ready and need to be added
    manually to Automation #9 "Lista email Broadcast".
    """
    text = (
        f"📧 <b>Nuovi template Brevo pronti — herbashop.it</b>\n\n"
        f"📌 <b>Argomento:</b> {textwrap.shorten(topic_title, width=80)}\n\n"
        f"<b>Template creati:</b>\n"
        f"  • <code>{template_1_name}</code> (id={template_1_id})\n"
        f"  • <code>{template_2_name}</code> (id={template_2_id})\n\n"
        f"⚠️ <b>Azione richiesta:</b>\n"
        f"Vai su <b>Brevo → Automazioni → Scenario #9</b> «Lista email Broadcast» "
        f"e aggiungi in fondo alla sequenza:\n"
        f"  1️⃣ Nodo <b>Attendi 14 giorni</b>\n"
        f"  2️⃣ Nodo <b>Invia email</b> → seleziona <code>{template_1_name}</code>\n"
        f"  3️⃣ Nodo <b>Attendi 14 giorni</b>\n"
        f"  4️⃣ Nodo <b>Invia email</b> → seleziona <code>{template_2_name}</code>\n\n"
        f"📁 I template si trovano in <b>Marketing → Modelli</b>"
    )
    for chat_id in _get_notify_chat_ids():
        asyncio.run(_send_message(text, chat_id))
        log.info(
            "telegram_notification_sent",
            type="brevo_templates_ready",
            template_1=template_1_name,
            template_2=template_2_name,
        )


def notify_error(context: str, error: str) -> None:
    """Send a generic error notification."""
    text = (
        f"🚨 <b>Errore HerbaMarketer</b>\n\n"
        f"<b>Contesto:</b> {context}\n"
        f"<b>Errore:</b> <code>{textwrap.shorten(error, width=300)}</code>"
    )
    for chat_id in _get_notify_chat_ids():
        asyncio.run(_send_message(text, chat_id))


def notify_topic_selection(topics: list) -> None:
    """
    Send a Telegram message asking Omar to choose a topic for the next article.
    Topics are presented as inline keyboard buttons.
    """
    if not topics:
        text = (
            "📌 <b>Selezione argomento quindicina</b>\n\n"
            "Nessun topic pending nel backlog.\n"
            "Aggiungi un argomento con /addtopic oppure scrivi direttamente /addtopic testo."
        )
        for chat_id in _get_notify_chat_ids():
            asyncio.run(_send_message(text, chat_id))
        return

    text = (
        "📌 <b>Scegli l'argomento per l'articolo di questa quindicina:</b>\n\n"
        + "\n".join(
            f"[{t.id}] P{t.priority} — {textwrap.shorten(t.title, width=60)}"
            for t in topics
        )
    )

    # Build one button per topic (max 2 per row)
    buttons = [
        InlineKeyboardButton(
            textwrap.shorten(t.title, width=30),
            callback_data=f"select_topic:{t.id}",
        )
        for t in topics
    ]
    # Group into rows of 2
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard = InlineKeyboardMarkup(rows)

    for chat_id in _get_notify_chat_ids():
        asyncio.run(_send_message(text, chat_id, reply_markup=keyboard))
    log.info("telegram_topic_selection_sent", topic_count=len(topics))


def notify_article_drafts_ready(
    topic_id: int,
    topic_title: str,
    draft_results: list[dict],
) -> None:
    """
    Notify that article drafts are ready on WordPress.

    draft_results: list of {site_slug, article_db_id, post_id, post_url}
    """
    lines = [
        f"📝 <b>Articoli pronti in bozza</b>\n",
        f"📌 <b>Argomento:</b> {textwrap.shorten(topic_title, width=80)}\n",
    ]
    for r in draft_results:
        lines.append(f"• <b>{r['site_slug']}</b> — <a href='{r['post_url']}'>anteprima</a>")

    text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Pubblica tutto",
                callback_data=f"publish_all_articles:{topic_id}",
            ),
            InlineKeyboardButton(
                "❌ Rigetta tutto",
                callback_data=f"reject_all_articles:{topic_id}",
            ),
        ],
    ])

    for chat_id in _get_notify_chat_ids():
        asyncio.run(_send_message(text, chat_id, reply_markup=keyboard))
    log.info(
        "telegram_article_drafts_ready_sent",
        topic_id=topic_id,
        sites=[r["site_slug"] for r in draft_results],
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — system overview."""
    db = SessionLocal()
    try:
        pending = db.query(ContentTopic).filter(ContentTopic.status == "pending").count()
        approved = db.query(ContentTopic).filter(ContentTopic.status == "approved").count()
        draft_pairs = db.query(EmailPair).filter(EmailPair.status == "draft").count()
        published_pairs = db.query(EmailPair).filter(EmailPair.status == "published").count()

        active_sites = get_all_active_sites()
        text = (
            f"📊 <b>HerbaMarketer — Stato sistema</b>\n\n"
            f"🌐 Siti attivi: {len(active_sites)}\n"
            f"📌 Topic pending: {pending}\n"
            f"✅ Topic approvati: {approved}\n"
            f"📧 Email pair in bozza: {draft_pairs}\n"
            f"🚀 Email pair pubblicate: {published_pairs}\n"
        )
    finally:
        db.close()

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/topics — list pending topics."""
    db = SessionLocal()
    try:
        topics = (
            db.query(ContentTopic)
            .filter(ContentTopic.status == "pending")
            .order_by(ContentTopic.priority.desc(), ContentTopic.created_at)
            .limit(10)
            .all()
        )
        if not topics:
            await update.message.reply_text("Nessun topic pending.", parse_mode="HTML")
            return

        lines = ["📌 <b>Topic pending (top 10):</b>\n"]
        for t in topics:
            lines.append(
                f"[{t.id}] P{t.priority} | {textwrap.shorten(t.title, width=60)}"
                f" <i>({t.source})</i>"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    finally:
        db.close()


async def cmd_addtopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addtopic <testo> — add a manual topic."""
    title = " ".join(context.args) if context.args else ""
    if not title:
        await update.message.reply_text("Uso: /addtopic <testo argomento>")
        return

    db = SessionLocal()
    try:
        topic = ContentTopic(title=title, source="manual", status="pending", priority=7)
        db.add(topic)
        db.commit()
        db.refresh(topic)
        await update.message.reply_text(
            f"✅ Topic aggiunto (id={topic.id}): {title}", parse_mode="HTML"
        )
        log.info("topic_added_via_telegram", topic_id=topic.id, title=title)
    finally:
        db.close()


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/approve <id> — approve a topic."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /approve <id>")
        return

    topic_id = int(context.args[0])
    db = SessionLocal()
    try:
        topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
        if not topic:
            await update.message.reply_text(f"Topic {topic_id} non trovato.")
            return
        topic.status = "approved"
        db.commit()
        await update.message.reply_text(
            f"✅ Topic {topic_id} approvato: {topic.title}", parse_mode="HTML"
        )
        log.info("topic_approved_via_telegram", topic_id=topic_id)
    finally:
        db.close()


async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/preview <id> — preview a generated email pair."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /preview <id>")
        return

    pair_id = int(context.args[0])
    db = SessionLocal()
    try:
        pair = db.query(EmailPair).filter(EmailPair.id == pair_id).first()
        if not pair:
            await update.message.reply_text(f"EmailPair {pair_id} non trovato.")
            return

        import html as html_lib

        # Strip HTML tags for preview
        def _strip_html(s: str) -> str:
            import re
            return re.sub(r"<[^>]+>", "", s or "")

        text = (
            f"👁 <b>Anteprima EmailPair #{pair_id}</b>\n\n"
            f"<b>Email 1 — Problema</b>\n"
            f"Oggetto: <i>{html_lib.escape(pair.email_1_subject or '')}</i>\n"
            f"{textwrap.shorten(_strip_html(pair.email_1_body or ''), width=300)}\n\n"
            f"<b>Email 2 — Prodotto</b>\n"
            f"Oggetto: <i>{html_lib.escape(pair.email_2_subject or '')}</i>\n"
            f"{textwrap.shorten(_strip_html(pair.email_2_body or ''), width=300)}"
        )
        await update.message.reply_text(text, parse_mode="HTML")
    finally:
        db.close()


async def cmd_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sites — status of each active site."""
    db = SessionLocal()
    try:
        lines = ["🌐 <b>Siti attivi:</b>\n"]
        for site_cfg in get_all_active_sites():
            site_db = db.query(Site).filter(Site.slug == site_cfg.slug).first()
            published = (
                db.query(EmailPair)
                .filter(EmailPair.site_id == site_db.id, EmailPair.status == "published")
                .count()
                if site_db else 0
            )
            lines.append(
                f"• <b>{site_cfg.slug}</b> ({site_cfg.language}) — "
                f"{published} email pubblicate | "
                f"campagna #{site_cfg.mautic_campaign_id}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    finally:
        db.close()


async def cmd_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/publish <article_db_id> — force-publish a specific article draft."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /publish <article_db_id>")
        return

    article_id = int(context.args[0])
    db = SessionLocal()
    try:
        article = db.query(Article).filter(Article.id == article_id).first()
        if not article:
            await update.message.reply_text(f"Articolo {article_id} non trovato.")
            return
        if not article.wp_post_id:
            await update.message.reply_text(
                f"Articolo {article_id} non ha un WP post ID associato."
            )
            return

        site_db = db.query(Site).filter(Site.id == article.site_id).first()
        if not site_db:
            await update.message.reply_text("Sito non trovato per questo articolo.")
            return

        site_cfg = get_site_config(site_db.slug)
        from publishers.wordpress import WordPressPublisher
        publisher = WordPressPublisher(site_cfg)
        publisher.publish_post(article.wp_post_id)

        article.status = "published"
        article.published_at = __import__("datetime").datetime.utcnow()
        db.commit()

        await update.message.reply_text(
            f"✅ Articolo {article_id} pubblicato su {site_db.slug} "
            f"(WP post #{article.wp_post_id})"
        )
        log.info("article_published_via_telegram", article_id=article_id)
    finally:
        db.close()


async def cmd_syncemail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/syncemail — manually trigger email ingestor to process unread forwarded emails."""
    from inputs.email_ingestor import run_email_ingestor
    await update.message.reply_text("📬 Lettura email in corso...")
    db = SessionLocal()
    try:
        topics = run_email_ingestor(db)
        if not topics:
            await update.message.reply_text("Nessuna email non letta trovata.")
        else:
            lines = [f"✅ {len(topics)} topic creati da email:\n"]
            for t in topics:
                lines.append(f"• [{t.id}] {textwrap.shorten(t.title, width=60)}")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except EnvironmentError as exc:
        await update.message.reply_text(
            f"❌ Configurazione mancante: {exc}\n"
            f"Imposta INGESTOR_EMAIL e INGESTOR_PASSWORD nel .env"
        )
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        short_tb = tb[-600:] if len(tb) > 600 else tb
        await update.message.reply_text(
            f"❌ Errore: {exc}\n\n<pre>{short_tb}</pre>",
            parse_mode="HTML",
        )
        log.error("syncemail_failed", error=str(exc), traceback=tb)
    finally:
        db.close()


async def cmd_ga4sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ga4sync — manually trigger GA4 analytics sync for all sites."""
    from core.analytics_sync import sync_all_sites_analytics

    await update.message.reply_text("📊 GA4 sync in corso...")
    try:
        results = sync_all_sites_analytics()
        lines = ["<b>GA4 Sync completato:</b>\n"]
        for slug, r in results.items():
            if r.get("success"):
                d = r["data"]
                lines.append(
                    f"✅ <b>{slug}</b>: {d.get('sessions', '?')} sessioni, "
                    f"{d.get('pageviews', '?')} pageviews, "
                    f"€{d.get('revenue', '?'):.0f} revenue"
                )
            else:
                err = r.get("error", "unknown")
                if err == "no ga4_property_id configured":
                    lines.append(f"⏭ <b>{slug}</b>: nessun property ID configurato")
                else:
                    lines.append(f"❌ <b>{slug}</b>: {err}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"❌ Errore GA4 sync: {exc}")
        log.error("ga4sync_telegram_failed", error=str(exc))


async def handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages that contain a URL — ingest as topic."""
    import re
    text = update.message.text or ""
    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        return  # not a URL message, ignore

    url = urls[0]
    await update.message.reply_text(f"🔍 Analizzo URL: {url}")

    from inputs.url_ingestor import ingest_url
    db = SessionLocal()
    try:
        topic = ingest_url(url, db)
        if topic:
            await update.message.reply_text(
                f"✅ Topic creato da URL:\n"
                f"<b>[{topic.id}]</b> {topic.title}\n\n"
                f"Usa /approve {topic.id} per approvarlo.",
                parse_mode="HTML",
            )
            log.info("url_topic_created_via_telegram", topic_id=topic.id, url=url)
        else:
            await update.message.reply_text(
                "❌ Non sono riuscito ad estrarre un topic dall'URL. "
                "Prova con un articolo che abbia contenuto testuale."
            )
    except Exception as exc:
        await update.message.reply_text(f"❌ Errore durante l'analisi: {exc}")
        log.error("url_ingest_telegram_failed", url=url, error=str(exc))
    finally:
        db.close()


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report — weekly summary."""
    from datetime import datetime, timedelta

    db = SessionLocal()
    try:
        week_ago = datetime.utcnow() - timedelta(days=7)
        new_topics = (
            db.query(ContentTopic).filter(ContentTopic.created_at >= week_ago).count()
        )
        published_pairs = (
            db.query(EmailPair)
            .filter(EmailPair.status == "published", EmailPair.published_at >= week_ago)
            .count()
        )
        failures = (
            db.query(PublishLog)
            .filter(PublishLog.action == "failed", PublishLog.created_at >= week_ago)
            .count()
        )
        text = (
            f"📋 <b>Report settimanale</b>\n\n"
            f"🗓 Ultimi 7 giorni\n"
            f"📌 Nuovi topic: {new_topics}\n"
            f"📧 Email pair pubblicate: {published_pairs}\n"
            f"❌ Errori pubblicazione: {failures}\n"
        )
        await update.message.reply_text(text, parse_mode="HTML")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Callback query handlers (inline buttons)
# ---------------------------------------------------------------------------


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data.startswith("approve_email:"):
        pair_id = int(data.split(":")[1])
        db = SessionLocal()
        try:
            pair = db.query(EmailPair).filter(EmailPair.id == pair_id).first()
            if pair:
                pair.status = "approved"
                db.commit()
                await query.edit_message_text(
                    f"✅ EmailPair #{pair_id} approvato — in coda per la pubblicazione."
                )
                log.info("email_pair_approved_via_telegram", pair_id=pair_id)
        finally:
            db.close()

    elif data.startswith("reject_email:"):
        pair_id = int(data.split(":")[1])
        db = SessionLocal()
        try:
            pair = db.query(EmailPair).filter(EmailPair.id == pair_id).first()
            if pair:
                pair.status = "failed"
                db.commit()
                await query.edit_message_text(f"❌ EmailPair #{pair_id} rifiutato.")
                log.info("email_pair_rejected_via_telegram", pair_id=pair_id)
        finally:
            db.close()

    elif data.startswith("select_topic:"):
        topic_id = int(data.split(":")[1])
        db = SessionLocal()
        try:
            topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
            if topic:
                topic.status = "approved"
                db.commit()
                await query.edit_message_text(
                    f"✅ Argomento selezionato: <b>{topic.title}</b>\n"
                    f"Verrà generato alla prossima run dell'article_job.",
                    parse_mode="HTML",
                )
                log.info("topic_selected_via_telegram", topic_id=topic_id)
        finally:
            db.close()

    elif data.startswith("publish_all_articles:"):
        topic_id = int(data.split(":")[1])
        db = SessionLocal()
        try:
            articles = (
                db.query(Article)
                .filter(
                    Article.topic_id == topic_id,
                    Article.status == "pending_approval",
                    Article.wp_post_id.isnot(None),
                )
                .all()
            )
            published = []
            failed = []
            for article in articles:
                site_db = db.query(Site).filter(Site.id == article.site_id).first()
                if not site_db:
                    continue
                try:
                    site_cfg = get_site_config(site_db.slug)
                    from publishers.wordpress import WordPressPublisher
                    WordPressPublisher(site_cfg).publish_post(article.wp_post_id)
                    article.status = "published"
                    article.published_at = __import__("datetime").datetime.utcnow()
                    published.append(site_db.slug)
                except Exception as exc:
                    failed.append(f"{site_db.slug}: {exc}")
            db.commit()

            result_text = f"✅ Pubblicati: {', '.join(published) or 'nessuno'}"
            if failed:
                result_text += f"\n❌ Errori: {'; '.join(failed)}"
            await query.edit_message_text(result_text)
            log.info(
                "articles_published_via_telegram",
                topic_id=topic_id,
                published=published,
            )
        finally:
            db.close()

    elif data.startswith("reject_all_articles:"):
        topic_id = int(data.split(":")[1])
        db = SessionLocal()
        try:
            db.query(Article).filter(
                Article.topic_id == topic_id,
                Article.status == "pending_approval",
            ).update({"status": "failed"})
            db.commit()
            await query.edit_message_text(
                f"❌ Articoli topic #{topic_id} rifiutati."
            )
            log.info("articles_rejected_via_telegram", topic_id=topic_id)
        finally:
            db.close()

    elif data.startswith("preview_email:"):
        pair_id = int(data.split(":")[1])
        db = SessionLocal()
        try:
            pair = db.query(EmailPair).filter(EmailPair.id == pair_id).first()
            if pair:
                import re
                def _strip(s: str) -> str:
                    return re.sub(r"<[^>]+>", "", s or "")
                preview = (
                    f"👁 <b>EmailPair #{pair_id}</b>\n\n"
                    f"Email 1: {pair.email_1_subject}\n"
                    f"{textwrap.shorten(_strip(pair.email_1_body or ''), 200)}\n\n"
                    f"Email 2: {pair.email_2_subject}\n"
                    f"{textwrap.shorten(_strip(pair.email_2_body or ''), 200)}"
                )
                await query.message.reply_text(preview, parse_mode="HTML")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------


def build_application() -> Application:
    """Build and configure the Telegram bot Application."""
    if not TELEGRAM_BOT_TOKEN:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("addtopic", cmd_addtopic))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("preview", cmd_preview))
    app.add_handler(CommandHandler("publish", cmd_publish))
    app.add_handler(CommandHandler("sites", cmd_sites))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("syncemail", cmd_syncemail))
    app.add_handler(CommandHandler("ga4sync", cmd_ga4sync))
    app.add_handler(CallbackQueryHandler(handle_callback))
    # URL messages: intercept plain text containing https?:// links
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url_message))

    return app


def run_bot() -> None:
    """Start the bot in long-polling mode (blocking)."""
    log.info("telegram_bot_starting")
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_bot()
