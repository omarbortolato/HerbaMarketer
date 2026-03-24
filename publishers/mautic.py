"""
publishers/mautic.py

Mautic API client for HerbaMarketer.

Responsibilities:
- OAuth2 token management (client_credentials, auto-refresh)
- Create email templates with BR_ naming convention
- Add emails as campaign events (+14d trigger)
- Idempotency: check if email name already exists before creating
- Retry logic: 3 attempts with 5s wait on transient errors

Naming convention (option A, consistent with existing):
  BR_{SITE_PREFIX}__{NNN}_{topic_slug}
  e.g. BR_IT__027_colazione_proteica
       BR_FR__027_colazione_proteique

Public API:
    MauticPublisher(site_config)
    publisher.publish_email_pair(email_pair, topic_slug, db_session) -> (email_1_id, email_2_id)
"""

import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

from agents.content_agent import EmailPairOutput
from config import SiteConfig, get_settings

log = structlog.get_logger(__name__)

# Map site slug → Mautic BR_ prefix
_SITE_PREFIX_MAP = {
    "herbago_it":    "IT",
    "herbago_fr":    "FR",
    "herbago_de":    "DE",
    "herbago_net":   "IE",
    "herbago_co_uk": "CO_UK",
    "hlifeus_com":   "USA",
    "herbashop_it":  "IT",   # herbashop uses Brevo, included for completeness
}


# ---------------------------------------------------------------------------
# Token cache (module-level, per base URL)
# ---------------------------------------------------------------------------

_token_cache: dict[str, dict] = {}  # {mautic_url: {access_token, expires_at}}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    email_1_mautic_id: int
    email_2_mautic_id: int
    email_1_name: str
    email_2_name: str
    campaign_event_1_id: int
    campaign_event_2_id: int


# ---------------------------------------------------------------------------
# MauticPublisher
# ---------------------------------------------------------------------------


class MauticPublisher:
    """
    Publishes email pairs to Mautic for a specific site.

    Usage:
        publisher = MauticPublisher(site_config)
        result = publisher.publish_email_pair(email_pair, topic_slug, db)
    """

    def __init__(self, site_config: SiteConfig) -> None:
        self.site = site_config
        self.base_url = os.getenv("MAUTIC_URL", "").rstrip("/")
        self.client_id = os.getenv("MAUTIC_CLIENT_ID", "")
        self.client_secret = os.getenv("MAUTIC_CLIENT_SECRET", "")
        self.settings = get_settings()

        if not self.base_url:
            raise EnvironmentError("MAUTIC_URL is not set")
        if not self.client_id or not self.client_secret:
            raise EnvironmentError("MAUTIC_CLIENT_ID / MAUTIC_CLIENT_SECRET are not set")

        self._site_prefix = _SITE_PREFIX_MAP.get(site_config.slug, site_config.slug.upper())

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a valid OAuth2 Bearer token, refreshing if expired."""
        cached = _token_cache.get(self.base_url, {})
        if cached and cached.get("expires_at", 0) > time.time() + 60:
            return cached["access_token"]

        log.info("mautic_token_refresh", base_url=self.base_url)
        resp = httpx.post(
            f"{self.base_url}/oauth/v2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache[self.base_url] = {
            "access_token": data["access_token"],
            "expires_at": time.time() + data.get("expires_in", 3600),
        }
        return data["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = httpx.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    def _post(self, path: str, payload: dict) -> dict:
        resp = httpx.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    def _patch(self, path: str, payload: dict) -> dict:
        resp = httpx.patch(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def _delay(self) -> None:
        """Rate-limit delay between consecutive Mautic API calls."""
        delay = self.settings.publishers.get("mautic_delay_seconds", 1)
        time.sleep(delay)

    # ------------------------------------------------------------------
    # Naming convention
    # ------------------------------------------------------------------

    def _make_email_name(self, sequence_number: int, topic_slug: str) -> str:
        """
        Build the Mautic email name using convention A:
        BR_{SITE_PREFIX}__{NNN}_{topic_slug}
        e.g. BR_IT__027_colazione_proteica
        """
        topic_short = re.sub(r"[^a-z0-9]+", "_", topic_slug.lower()).strip("_")[:40]
        return f"BR_{self._site_prefix}__{sequence_number:03d}_{topic_short}"

    # ------------------------------------------------------------------
    # Campaign inspection
    # ------------------------------------------------------------------

    def _get_campaign_events(self) -> list[dict]:
        """Return the list of events in the site's campaign, ordered by sequence."""
        data = self._get(f"/api/campaigns/{self.site.mautic_campaign_id}")
        events = data.get("campaign", {}).get("events", [])
        if isinstance(events, dict):
            events = list(events.values())
        return sorted(events, key=lambda e: e.get("order", 0))

    def _get_next_sequence_number(self, events: list[dict]) -> int:
        """Return the next email sequence number (max existing order + 1)."""
        if not events:
            return 1
        return max(e.get("order", 0) for e in events) + 1

    def _check_email_exists(self, email_name: str) -> Optional[int]:
        """
        Check if an email with this name already exists (idempotency).
        Returns the Mautic email ID if found, None otherwise.
        """
        data = self._get(
            "/api/emails",
            params={"search": email_name, "limit": 5},
        )
        for em in (data.get("emails") or {}).values():
            if em.get("name") == email_name:
                return int(em["id"])
        return None

    # ------------------------------------------------------------------
    # Create email
    # ------------------------------------------------------------------

    def _create_email(
        self,
        name: str,
        subject: str,
        preheader: str,
        body_html: str,
        language: str,
    ) -> int:
        """
        Create a template email on Mautic.
        Returns the new email ID.
        """
        # Idempotency check
        existing_id = self._check_email_exists(name)
        if existing_id:
            log.info("mautic_email_already_exists", name=name, id=existing_id)
            return existing_id

        payload = {
            "name": name,
            "subject": subject,
            "preheaderText": preheader,
            "customHtml": body_html,
            "emailType": "template",
            "language": language,
            "isPublished": True,
        }

        data = self._post("/api/emails/new", payload)
        email_id = int(data["email"]["id"])
        log.info("mautic_email_created", name=name, id=email_id)
        return email_id

    # ------------------------------------------------------------------
    # Add campaign event
    # ------------------------------------------------------------------

    def _add_campaign_event(
        self,
        email_id: int,
        event_name: str,
        order: int,
    ) -> int:
        """
        Append a new email.send event to the campaign.

        Mautic requires sending the full campaign payload to add an event.
        We fetch the current campaign, append the new event, and PATCH it.

        Returns the new campaign event ID.
        """
        campaign_data = self._get(f"/api/campaigns/{self.site.mautic_campaign_id}")
        campaign = campaign_data.get("campaign", {})
        existing_events = campaign.get("events", [])
        if isinstance(existing_events, dict):
            existing_events = list(existing_events.values())

        new_event = {
            "name": event_name,
            "type": "email.send",
            "eventType": "action",
            "channel": "email",
            "channelId": email_id,
            "order": order,
            "properties": {
                "email": str(email_id),
                "email_type": "marketing",
                "priority": "2",
                "attempts": "3",
            },
            "triggerInterval": "14",
            "triggerIntervalUnit": "d",
            "triggerMode": "interval",
        }

        # Build payload: Mautic expects events as list
        updated_events = existing_events + [new_event]

        patch_payload = {
            "events": updated_events,
        }

        result = self._patch(
            f"/api/campaigns/{self.site.mautic_campaign_id}/edit",
            patch_payload,
        )

        # Find the newly created event by matching channelId and order
        updated_campaign = result.get("campaign", {})
        result_events = updated_campaign.get("events", [])
        if isinstance(result_events, dict):
            result_events = list(result_events.values())

        new_ev = next(
            (e for e in result_events
             if int(e.get("channelId", 0)) == email_id and e.get("order") == order),
            None,
        )
        event_id = int(new_ev["id"]) if new_ev else 0

        log.info(
            "mautic_campaign_event_added",
            campaign_id=self.site.mautic_campaign_id,
            email_id=email_id,
            order=order,
            event_id=event_id,
        )
        return event_id

    # ------------------------------------------------------------------
    # Public: publish email pair
    # ------------------------------------------------------------------

    def publish_email_pair(
        self,
        email_pair: EmailPairOutput,
        topic_slug: str,
    ) -> PublishResult:
        """
        Publish an email pair to Mautic:
        1. Create email 1 (problem)
        2. Create email 2 (product)
        3. Add both as campaign events in sequence

        Idempotent: if emails already exist, skips creation.

        Args:
            email_pair:  Translated EmailPairOutput for this site.
            topic_slug:  Short slug used in the email name.

        Returns:
            PublishResult with Mautic IDs.
        """
        log.info(
            "mautic_publish_start",
            site=self.site.slug,
            campaign_id=self.site.mautic_campaign_id,
        )

        # Get current campaign state
        events = self._get_campaign_events()
        next_seq = self._get_next_sequence_number(events)

        name_1 = self._make_email_name(next_seq, topic_slug)
        name_2 = self._make_email_name(next_seq + 1, topic_slug)

        # Create email 1
        self._delay()
        email_1_id = self._create_email(
            name=name_1,
            subject=email_pair.email_1.subject,
            preheader=email_pair.email_1.preheader,
            body_html=email_pair.email_1.body_html,
            language=email_pair.language,
        )

        self._delay()

        # Create email 2
        email_2_id = self._create_email(
            name=name_2,
            subject=email_pair.email_2.subject,
            preheader=email_pair.email_2.preheader,
            body_html=email_pair.email_2.body_html,
            language=email_pair.language,
        )

        self._delay()

        # Add campaign events
        event_1_id = self._add_campaign_event(
            email_id=email_1_id,
            event_name=f"Email {next_seq}",
            order=next_seq,
        )

        self._delay()

        event_2_id = self._add_campaign_event(
            email_id=email_2_id,
            event_name=f"Email {next_seq + 1}",
            order=next_seq + 1,
        )

        result = PublishResult(
            email_1_mautic_id=email_1_id,
            email_2_mautic_id=email_2_id,
            email_1_name=name_1,
            email_2_name=name_2,
            campaign_event_1_id=event_1_id,
            campaign_event_2_id=event_2_id,
        )

        log.info(
            "mautic_publish_complete",
            site=self.site.slug,
            email_1=f"{name_1} (id={email_1_id})",
            email_2=f"{name_2} (id={email_2_id})",
        )

        return result
