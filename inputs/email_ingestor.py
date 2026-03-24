"""
inputs/email_ingestor.py

IMAP email ingestor for HerbaMarketer.

Connects to a Gmail/IMAP inbox, reads unread emails, extracts content
topics via Claude, and saves them as ContentTopic entries in the DB.

Designed for forwarded emails containing articles, newsletters, or
research that Omar wants to turn into blog/email content.

Public API:
    run_email_ingestor(db) -> list[ContentTopic]

Required env vars:
    INGESTOR_EMAIL        — inbox address (e.g. omar@gmail.com)
    INGESTOR_PASSWORD     — Gmail app password or IMAP password
    INGESTOR_IMAP_HOST    — IMAP server (default: imap.gmail.com)
    INGESTOR_IMAP_PORT    — IMAP SSL port (default: 993)
"""

import email
import imaplib
import os
import re
from email.header import decode_header
from typing import Optional

import structlog

from agents.content_agent import _call_claude
from core.database import ContentTopic

log = structlog.get_logger(__name__)

_MAX_BODY_CHARS = 3000
_IMAP_HOST_DEFAULT = "imap.gmail.com"
_IMAP_PORT_DEFAULT = 993


# ---------------------------------------------------------------------------
# IMAP / email helpers
# ---------------------------------------------------------------------------


def _decode_header_str(raw: str | bytes, encoding: Optional[str] = None) -> str:
    if isinstance(raw, bytes):
        return raw.decode(encoding or "utf-8", errors="replace")
    return raw


def _decode_subject(msg: email.message.Message) -> str:
    parts = decode_header(msg.get("Subject", ""))
    return " ".join(
        _decode_header_str(part, enc) for part, enc in parts
    ).strip()


def _extract_body(msg: email.message.Message) -> str:
    """Extract plain-text body from an email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if (
                part.get_content_type() == "text/plain"
                and not part.get("Content-Disposition")
            ):
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                body = payload.decode(charset, errors="replace") if payload else ""
                break
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        body = payload.decode(charset, errors="replace") if payload else ""

    # Normalize whitespace / excessive blank lines
    body = re.sub(r"\n{3,}", "\n\n", body.strip())
    return body[:_MAX_BODY_CHARS]


# ---------------------------------------------------------------------------
# Claude topic extraction
# ---------------------------------------------------------------------------


def _extract_topic(subject: str, body: str) -> dict:
    """
    Ask Claude to extract a blog topic from a forwarded email.
    Returns dict with 'title' and 'keyword'.
    """
    messages = [
        {
            "role": "user",
            "content": (
                f"Analizza questa email che mi è stata inoltrata:\n\n"
                f"Oggetto: {subject}\n\n"
                f"Corpo:\n{body}\n\n"
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
    return _call_claude(messages, system, context="email_ingestor")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_email_ingestor(db) -> list[ContentTopic]:
    """
    Fetch unread emails via IMAP, extract topics, and save to DB.

    Each processed email is marked as read (\\Seen flag).

    Args:
        db: SQLAlchemy session.

    Returns:
        List of ContentTopic objects created in this run.

    Raises:
        EnvironmentError: if INGESTOR_EMAIL or INGESTOR_PASSWORD not set.
        imaplib.IMAP4.error: on connection / authentication failure.
    """
    ingestor_email = os.getenv("INGESTOR_EMAIL", "")
    ingestor_password = os.getenv("INGESTOR_PASSWORD", "")
    imap_host = os.getenv("INGESTOR_IMAP_HOST", _IMAP_HOST_DEFAULT)
    imap_port = int(os.getenv("INGESTOR_IMAP_PORT", str(_IMAP_PORT_DEFAULT)))

    if not ingestor_email or not ingestor_password:
        raise EnvironmentError(
            "INGESTOR_EMAIL / INGESTOR_PASSWORD are not set"
        )

    log.info("email_ingestor_start", host=imap_host, email=ingestor_email)
    created: list[ContentTopic] = []

    mail = imaplib.IMAP4_SSL(imap_host, imap_port)
    try:
        mail.login(ingestor_email, ingestor_password)
        mail.select("INBOX")

        _, msg_nums = mail.search(None, "UNSEEN")
        ids = msg_nums[0].split() if msg_nums[0] else []
        log.info("email_ingestor_unread", count=len(ids))

        for num in ids:
            try:
                _, data = mail.fetch(num, "(RFC822)")
                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                subject = _decode_subject(msg)
                body = _extract_body(msg)
                if not body.strip():
                    log.debug("email_empty_body", subject=subject)
                    continue

                result = _extract_topic(subject, body)
                title = result.get("title") or subject
                keyword = result.get("keyword") or ""

                topic = ContentTopic(
                    title=title,
                    source="email_input",
                    source_detail=f"Subject: {subject} | keyword: {keyword}",
                    status="pending",
                    priority=6,
                )
                db.add(topic)
                db.commit()
                db.refresh(topic)

                # Mark email as read
                mail.store(num, "+FLAGS", "\\Seen")
                created.append(topic)
                log.info(
                    "email_topic_created",
                    topic_id=topic.id,
                    subject=subject,
                    title=title,
                )

            except Exception as exc:
                log.error("email_processing_failed", num=num, error=str(exc))
                continue

    finally:
        try:
            mail.logout()
        except Exception:
            pass

    log.info("email_ingestor_complete", created=len(created))
    return created
