"""
tests/test_publishers.py

Unit tests for publishers/mautic.py.
All HTTP calls are mocked — no real Mautic API calls.

Run with:
    pytest tests/test_publishers.py -v
"""

import os
import time
from unittest.mock import MagicMock, patch, call

import pytest

from agents.content_agent import EmailContent, EmailPairOutput
from config import SiteConfig
from publishers.mautic import MauticPublisher, PublishResult, _SITE_PREFIX_MAP


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def herbago_it_config() -> SiteConfig:
    return SiteConfig(
        slug="herbago_it",
        url="https://herbago.it",
        language="it",
        locale="it-IT",
        platform="mautic",
        mautic_campaign_id=4,
        email_prefix="ITA",
        preferred_customer_url="https://www.hlifeclienteprivilegiato.it/",
        distributor_url="https://www.hl-distributor.com/it/",
        active=True,
    )


@pytest.fixture
def herbago_fr_config() -> SiteConfig:
    return SiteConfig(
        slug="herbago_fr",
        url="https://herbago.fr",
        language="fr",
        locale="fr-FR",
        platform="mautic",
        mautic_campaign_id=5,
        email_prefix="FR",
        preferred_customer_url="https://www.hlifepreferredcustomer.com/fr/",
        distributor_url="https://www.hl-distributor.com/fr/",
        active=True,
    )


@pytest.fixture
def sample_email_pair() -> EmailPairOutput:
    body = "<p>Ciao {contactfield=firstname},</p>" + "<p>Testo di esempio. </p>" * 40
    return EmailPairOutput(
        email_1=EmailContent(
            subject="Hai fame a metà mattina?",
            preheader="Scopri il motivo",
            body_html=body,
            body_text="Testo plain text",
        ),
        email_2=EmailContent(
            subject="Formula 1: colazione pronta in 2 min",
            preheader="Energia e sazietà garantite",
            body_html=body,
            body_text="Testo plain text",
        ),
        language="it",
        site_slug="herbago_it",
    )


@pytest.fixture(autouse=True)
def clear_token_cache():
    """Clear Mautic token cache before every test to avoid cross-test state."""
    import publishers.mautic as mautic_module
    mautic_module._token_cache.clear()
    yield
    mautic_module._token_cache.clear()


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("MAUTIC_URL", "https://broadcast.herbago.info")
    monkeypatch.setenv("MAUTIC_CLIENT_ID", "test_client_id")
    monkeypatch.setenv("MAUTIC_CLIENT_SECRET", "test_client_secret")


def _make_token_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"access_token": "fake_token", "expires_in": 3600}
    mock.raise_for_status = MagicMock()
    return mock


def _make_email_create_response(email_id: int, name: str):
    mock = MagicMock()
    mock.status_code = 200
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"email": {"id": email_id, "name": name}}
    return mock


def _make_campaign_response(events: list):
    mock = MagicMock()
    mock.status_code = 200
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "campaign": {
            "id": 4,
            "name": "Broadcast Herbago.it",
            "events": events,
        }
    }
    return mock


def _make_search_response(emails: dict):
    mock = MagicMock()
    mock.status_code = 200
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"emails": emails, "total": len(emails)}
    return mock


def _make_patch_response(events_after: list):
    mock = MagicMock()
    mock.status_code = 200
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "campaign": {
            "id": 4,
            "events": events_after,
        }
    }
    return mock


# ---------------------------------------------------------------------------
# Tests: naming convention
# ---------------------------------------------------------------------------


class TestNamingConvention:

    def test_it_prefix(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        assert publisher._make_email_name(27, "colazione proteica") == "BR_IT__027_colazione_proteica"

    def test_fr_prefix(self, herbago_fr_config, mock_env):
        publisher = MauticPublisher(herbago_fr_config)
        assert publisher._make_email_name(1, "petit dejeuner") == "BR_FR__001_petit_dejeuner"

    def test_sequence_number_zero_padded(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        assert publisher._make_email_name(5, "test") == "BR_IT__005_test"
        assert publisher._make_email_name(100, "test") == "BR_IT__100_test"

    def test_topic_slug_truncated_at_40(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        long_topic = "questo e un argomento molto lungo che supera i quaranta caratteri"
        name = publisher._make_email_name(1, long_topic)
        slug_part = name.split("__001_")[1]
        assert len(slug_part) <= 40

    def test_special_chars_replaced(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        name = publisher._make_email_name(1, "colazione: energia & salute!")
        assert ":" not in name
        assert "&" not in name
        assert "!" not in name

    def test_all_sites_have_prefix_mapping(self):
        expected_slugs = [
            "herbago_it", "herbago_fr", "herbago_de",
            "herbago_net", "herbago_co_uk", "hlifeus_com",
        ]
        for slug in expected_slugs:
            assert slug in _SITE_PREFIX_MAP, f"{slug} missing from _SITE_PREFIX_MAP"


# ---------------------------------------------------------------------------
# Tests: sequence number detection
# ---------------------------------------------------------------------------


class TestSequenceNumber:

    def test_next_sequence_empty_campaign(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        assert publisher._get_next_sequence_number([]) == 1

    def test_next_sequence_after_existing(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        events = [{"order": i} for i in range(1, 27)]
        assert publisher._get_next_sequence_number(events) == 27

    def test_next_sequence_single_event(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        assert publisher._get_next_sequence_number([{"order": 5}]) == 6


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:

    def test_returns_existing_id_if_email_exists(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        existing_response = _make_search_response({
            "1": {"id": 99, "name": "BR_IT__027_colazione_proteica"}
        })

        with patch.object(publisher, "_get_token", return_value="fake_token"), \
             patch("publishers.mautic.httpx.get", return_value=existing_response):
            result = publisher._check_email_exists("BR_IT__027_colazione_proteica")

        assert result == 99

    def test_returns_none_if_email_not_found(self, herbago_it_config, mock_env):
        publisher = MauticPublisher(herbago_it_config)
        empty_response = _make_search_response({})

        with patch.object(publisher, "_get_token", return_value="fake_token"), \
             patch("publishers.mautic.httpx.get", return_value=empty_response):
            result = publisher._check_email_exists("BR_IT__999_nonexistent")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: publish_email_pair (full flow)
# ---------------------------------------------------------------------------


class TestPublishEmailPair:

    def _setup_mock_sequence(self, herbago_it_config, sample_email_pair, mock_env):
        """
        Set up mock objects for publish_email_pair.
        Token is patched at _get_token level so httpx.post only handles email creates.
        """
        publisher = MauticPublisher(herbago_it_config)

        existing_events = [{"order": i, "channelId": i + 60} for i in range(1, 27)]

        campaign_get = _make_campaign_response(existing_events)
        search_empty = _make_search_response({})
        email1_create = _make_email_create_response(236, "BR_IT__027_colazione_proteica")
        email2_create = _make_email_create_response(237, "BR_IT__028_colazione_proteica")
        events_after_1 = existing_events + [{"order": 27, "channelId": 236, "id": 101}]
        events_after_2 = events_after_1 + [{"order": 28, "channelId": 237, "id": 102}]
        patch_resp_1 = _make_patch_response(events_after_1)
        patch_resp_2 = _make_patch_response(events_after_2)

        return publisher, campaign_get, search_empty, email1_create, email2_create, patch_resp_1, patch_resp_2

    def test_publish_returns_correct_ids(
        self, herbago_it_config, sample_email_pair, mock_env
    ):
        publisher, campaign_get, search_empty, email1_create, email2_create, patch1, patch2 = (
            self._setup_mock_sequence(herbago_it_config, sample_email_pair, mock_env)
        )

        with patch.object(publisher, "_get_token", return_value="fake_token"), \
             patch("publishers.mautic.httpx.post") as mock_post, \
             patch("publishers.mautic.httpx.get") as mock_get, \
             patch("publishers.mautic.httpx.patch") as mock_patch, \
             patch("publishers.mautic.time.sleep"):

            mock_post.side_effect = [email1_create, email2_create]
            mock_get.side_effect = [campaign_get, search_empty, search_empty, campaign_get, campaign_get]
            mock_patch.side_effect = [patch1, patch2]

            result = publisher.publish_email_pair(sample_email_pair, "colazione_proteica")

        assert isinstance(result, PublishResult)
        assert result.email_1_mautic_id == 236
        assert result.email_2_mautic_id == 237

    def test_publish_uses_correct_naming(
        self, herbago_it_config, sample_email_pair, mock_env
    ):
        publisher, campaign_get, search_empty, email1_create, email2_create, patch1, patch2 = (
            self._setup_mock_sequence(herbago_it_config, sample_email_pair, mock_env)
        )

        with patch.object(publisher, "_get_token", return_value="fake_token"), \
             patch("publishers.mautic.httpx.post") as mock_post, \
             patch("publishers.mautic.httpx.get") as mock_get, \
             patch("publishers.mautic.httpx.patch") as mock_patch, \
             patch("publishers.mautic.time.sleep"):

            mock_post.side_effect = [email1_create, email2_create]
            mock_get.side_effect = [campaign_get, search_empty, search_empty, campaign_get, campaign_get]
            mock_patch.side_effect = [patch1, patch2]

            result = publisher.publish_email_pair(sample_email_pair, "colazione_proteica")

        assert result.email_1_name == "BR_IT__027_colazione_proteica"
        assert result.email_2_name == "BR_IT__028_colazione_proteica"

    def test_skips_email_creation_if_already_exists(
        self, herbago_it_config, sample_email_pair, mock_env
    ):
        """If email already exists (idempotency), POST is not called at all."""
        publisher = MauticPublisher(herbago_it_config)
        existing_events = [{"order": i, "channelId": i + 60} for i in range(1, 27)]

        campaign_get = _make_campaign_response(existing_events)
        # Both emails already exist
        search_found_1 = _make_search_response({"1": {"id": 236, "name": "BR_IT__027_colazione_proteica"}})
        search_found_2 = _make_search_response({"1": {"id": 237, "name": "BR_IT__028_colazione_proteica"}})
        events_after = existing_events + [
            {"order": 27, "channelId": 236, "id": 101},
            {"order": 28, "channelId": 237, "id": 102},
        ]
        patch_resp = _make_patch_response(events_after)

        with patch.object(publisher, "_get_token", return_value="fake_token"), \
             patch("publishers.mautic.httpx.post") as mock_post, \
             patch("publishers.mautic.httpx.get") as mock_get, \
             patch("publishers.mautic.httpx.patch") as mock_patch, \
             patch("publishers.mautic.time.sleep"):

            mock_post.side_effect = []  # no email creates since both already exist
            mock_get.side_effect = [campaign_get, search_found_1, search_found_2, campaign_get, campaign_get]
            mock_patch.side_effect = [patch_resp, patch_resp]

            result = publisher.publish_email_pair(sample_email_pair, "colazione_proteica")

        # POST not called at all — no email creation needed
        assert mock_post.call_count == 0
        assert result.email_1_mautic_id == 236
        assert result.email_2_mautic_id == 237

    def test_raises_on_missing_env_vars(self, herbago_it_config):
        """Raises EnvironmentError when MAUTIC credentials are missing."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ["MAUTIC_URL"] = "https://broadcast.herbago.info"
            # Missing CLIENT_ID and CLIENT_SECRET
            with pytest.raises(EnvironmentError, match="MAUTIC_CLIENT"):
                MauticPublisher(herbago_it_config)

    def test_raises_on_missing_mautic_url(self, herbago_it_config):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="MAUTIC_URL"):
                MauticPublisher(herbago_it_config)


# ---------------------------------------------------------------------------
# Tests: sitemap integration
# ---------------------------------------------------------------------------


class TestSitemapLookup:

    def test_find_product_url_formula1(self):
        from config import get_site_config
        from core.sitemap import find_product_url, get_product_urls
        get_product_urls.cache_clear()

        mock_urls = [
            "https://www.herbago.it/p/formula-1-sostituto-del-pasto-vaniglia-creme-550-g/",
            "https://www.herbago.it/p/cucchiaio-per-formula-1-e-protein-drink-mix/",
            "https://www.herbago.it/p/herbalifeline-max-30-capsule/",
        ]

        with patch("core.sitemap.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "\n".join(f"<loc>{u}</loc>" for u in mock_urls)
            mock_get.return_value = mock_resp

            from config import SiteConfig
            site = SiteConfig(slug="herbago_it", url="https://herbago.it",
                              language="it", locale="it-IT", platform="mautic")
            result = find_product_url("Formula 1 Herbalife", site)

        # Should match the actual product, not the spoon/accessory
        assert result is not None
        assert "formula-1-sostituto" in result
        assert "cucchiaio" not in result

    def test_find_product_url_returns_none_when_no_match(self):
        from core.sitemap import find_product_url, get_product_urls
        get_product_urls.cache_clear()

        mock_urls = [
            "https://www.herbago.it/p/totally-unrelated-product/",
        ]

        with patch("core.sitemap.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "\n".join(f"<loc>{u}</loc>" for u in mock_urls)
            mock_get.return_value = mock_resp

            from config import SiteConfig
            site = SiteConfig(slug="herbago_it", url="https://herbago.it",
                              language="it", locale="it-IT", platform="mautic")
            result = find_product_url("Formula 1 Herbalife", site, min_score=20)

        assert result is None
