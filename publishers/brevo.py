"""
publishers/brevo.py

Brevo (ex-Sendinblue) API client for HerbaMarketer.

Used for herbashop.it — a Brevo-based newsletter site.

Responsibilities:
- Create email TEMPLATES (one per email in the pair)
- Templates are added manually to Automation #9 "Lista email Broadcast"
- Idempotency: check if a template with the same name already exists

Why templates and not campaigns:
  Herbashop.it uses a Brevo automation sequence (Scenario #9) with ~26 emails
  chained via "wait 14 days → send email" nodes. Adding steps to an existing
  automation via API risks corrupting the sequence. Instead, HerbaMarketer
  creates templates and notifies Omar via Telegram to add them manually
  (30 seconds in the Brevo visual editor).

Naming convention:
  HS_IT_{NNN}_{topic_slug}
  e.g. HS_IT_027_colazione_proteica

Authentication: API key via header 'api-key: {BREVO_API_KEY}'.

Public API:
    BrevoPublisher(site_config)
    publisher.publish_email_pair(email_pair, topic_slug) -> BrevoPublishResult

Required env vars:
    BREVO_API_KEY         — Brevo API key
    BREVO_SENDER_NAME     — Sender display name (default: "HerbaShop")
    BREVO_SENDER_EMAIL    — Verified sender email (required)
"""

import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from agents.content_agent import EmailPairOutput
from config import SiteConfig, get_settings

log = structlog.get_logger(__name__)

_BREVO_URL = "https://api.brevo.com/v3"

# Sequence counter prefix per site slug
_BREVO_PREFIX_MAP = {
    "herbashop_it": "HS_IT",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BrevoPublishResult:
    template_1_id: int
    template_2_id: int
    template_1_name: str
    template_2_name: str


# ---------------------------------------------------------------------------
# BrevoPublisher
# ---------------------------------------------------------------------------


class BrevoPublisher:
    """
    Creates Brevo email templates for a specific site.

    Templates must be added manually to the Brevo automation sequence.
    A Telegram notification with instructions is sent after creation.

    Usage:
        publisher = BrevoPublisher(site_config)
        result = publisher.publish_email_pair(email_pair, topic_slug)
    """

    def __init__(self, site_config: SiteConfig) -> None:
        self.site = site_config
        self.api_key = os.getenv("BREVO_API_KEY", "")
        self.sender_name = os.getenv("BREVO_SENDER_NAME", "HerbaShop")
        self.sender_email = os.getenv("BREVO_SENDER_EMAIL", "")
        self.settings = get_settings()

        if not self.api_key:
            raise EnvironmentError("BREVO_API_KEY is not set")
        if not self.sender_email:
            raise EnvironmentError("BREVO_SENDER_EMAIL is not set")
        if not site_config.brevo_list_id:
            raise EnvironmentError(
                f"brevo_list_id not configured for site {site_config.slug}"
            )

        self._prefix = _BREVO_PREFIX_MAP.get(site_config.slug, "HS")

    def _headers(self) -> dict:
        return {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def _delay(self) -> None:
        delay = self.settings.publishers.get("mautic_delay_seconds", 1)
        time.sleep(delay)

    # ------------------------------------------------------------------
    # Naming
    # ------------------------------------------------------------------

    def _make_template_name(self, sequence_number: int, topic_slug: str) -> str:
        """
        Build the Brevo template name.
        Pattern: HS_IT_{NNN}_{topic_slug}
        e.g. HS_IT_027_colazione_proteica
        """
        slug = re.sub(r"[^a-z0-9]+", "_", topic_slug.lower()).strip("_")[:40]
        return f"{self._prefix}_{sequence_number:03d}_{slug}"

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    def _list_templates(self) -> list[dict]:
        """Return all existing email templates (for idempotency + sequence detection)."""
        results = []
        offset = 0
        limit = 50
        while True:
            resp = httpx.get(
                f"{_BREVO_URL}/smtp/templates",
                headers=self._headers(),
                params={"limit": limit, "offset": offset, "sort": "desc"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            templates = data.get("templates", [])
            results.extend(templates)
            if len(templates) < limit:
                break
            offset += limit
        return results

    def _find_existing_pair(self, topic_slug: str) -> Optional[tuple[int, int, str, str]]:
        """
        Look for an existing template pair with this topic slug.
        Returns (id_1, id_2, name_1, name_2) if both found, else None.
        """
        slug = re.sub(r"[^a-z0-9]+", "_", topic_slug.lower()).strip("_")[:40]
        pattern = re.compile(rf"^{re.escape(self._prefix)}_(\d+)_{re.escape(slug)}$")
        matches: dict[int, tuple[int, str]] = {}  # seq -> (id, name)
        for tmpl in self._list_templates():
            name = tmpl.get("name", "")
            m = pattern.match(name)
            if m:
                seq = int(m.group(1))
                matches[seq] = (int(tmpl["id"]), name)
        for seq in sorted(matches):
            if seq + 1 in matches:
                id1, name1 = matches[seq]
                id2, name2 = matches[seq + 1]
                return id1, id2, name1, name2
        return None

    def _get_next_sequence_number(self) -> int:
        """
        Detect the next sequence number from existing template names.
        Looks for templates matching the site prefix pattern.
        """
        max_seq = 0
        pattern = re.compile(rf"^{re.escape(self._prefix)}_(\d+)_")
        for tmpl in self._list_templates():
            name = tmpl.get("name", "")
            match = pattern.match(name)
            if match:
                seq = int(match.group(1))
                max_seq = max(max_seq, seq)
        return max_seq + 1

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    def _create_template(
        self,
        name: str,
        subject: str,
        html_content: str,
    ) -> int:
        """
        Create a Brevo email template.
        Returns the new template ID.
        """
        payload = {
            "templateName": name,
            "subject": subject,
            "htmlContent": html_content,
            "sender": {
                "name": self.sender_name,
                "email": self.sender_email,
            },
            "isActive": True,
        }

        resp = httpx.post(
            f"{_BREVO_URL}/smtp/templates",
            headers=self._headers(),
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        template_id = int(resp.json()["id"])
        log.info("brevo_template_created", name=name, id=template_id)
        return template_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def publish_email_pair(
        self,
        email_pair: EmailPairOutput,
        topic_slug: str,
    ) -> BrevoPublishResult:
        """
        Create two Brevo email templates for an email pair.

        Idempotent: if templates already exist, returns their IDs without
        creating duplicates.

        After creation, notify via Telegram to add the templates manually
        to Brevo Automation #9 with a "wait 14 days" node before each.

        Args:
            email_pair:  Translated EmailPairOutput for this site.
            topic_slug:  Short slug used in template names.

        Returns:
            BrevoPublishResult with template IDs and names.
        """
        log.info(
            "brevo_publish_start",
            site=self.site.slug,
            list_id=self.site.brevo_list_id,
        )

        # Idempotency: check if templates for this topic already exist
        existing_pair = self._find_existing_pair(topic_slug)
        if existing_pair:
            template_1_id, template_2_id, name_1, name_2 = existing_pair
            log.info(
                "brevo_template_pair_already_exists",
                name_1=name_1,
                name_2=name_2,
            )
        else:
            next_seq = self._get_next_sequence_number()
            name_1 = self._make_template_name(next_seq, topic_slug)
            name_2 = self._make_template_name(next_seq + 1, topic_slug)

            self._delay()
            template_1_id = self._create_template(
                name=name_1,
                subject=email_pair.email_1.subject,
                html_content=email_pair.email_1.body_html,
            )

            self._delay()
            template_2_id = self._create_template(
                name=name_2,
                subject=email_pair.email_2.subject,
                html_content=email_pair.email_2.body_html,
            )

        result = BrevoPublishResult(
            template_1_id=template_1_id,
            template_2_id=template_2_id,
            template_1_name=name_1,
            template_2_name=name_2,
        )

        log.info(
            "brevo_publish_complete",
            site=self.site.slug,
            template_1=f"{name_1} (id={template_1_id})",
            template_2=f"{name_2} (id={template_2_id})",
        )

        return result
