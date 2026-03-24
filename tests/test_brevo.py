"""
tests/test_brevo.py

Unit tests for publishers/brevo.py.
All HTTP calls are mocked — no real Brevo API calls.

Run with: pytest tests/test_brevo.py -v
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from agents.content_agent import EmailContent, EmailPairOutput
from config import SiteConfig
from publishers.brevo import BrevoPublisher, BrevoPublishResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def herbashop_it_config() -> SiteConfig:
    return SiteConfig(
        slug="herbashop_it",
        url="https://herbashop.it",
        language="it",
        locale="it-IT",
        platform="brevo",
        brevo_list_id=9,
        wp_api_url="https://herbashop.it/wp-json/wp/v2",
        wp_user_env="WP_HERBASHOP_IT_USER",
        wp_password_env="WP_HERBASHOP_IT_APP_PASSWORD",
        preferred_customer_url="https://www.hlifeclienteprivilegiato.it/",
        distributor_url="https://www.hl-distributor.com/it/",
        active=True,
    )


@pytest.fixture
def mock_brevo_env(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "xkeysib-test123")
    monkeypatch.setenv("BREVO_SENDER_NAME", "HerbaShop")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "info@herbashop.it")


@pytest.fixture
def sample_email_pair() -> EmailPairOutput:
    body = "<p>Ciao {contactfield=firstname},</p>" + "<p>Testo email.</p>" * 40
    return EmailPairOutput(
        email_1=EmailContent(
            subject="Hai fame a metà mattina?",
            preheader="Scopri il motivo",
            body_html=body,
            body_text="Plain text",
        ),
        email_2=EmailContent(
            subject="Formula 1: colazione in 2 min",
            preheader="Energia garantita",
            body_html=body,
            body_text="Plain text",
        ),
        language="it",
        site_slug="herbashop_it",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_templates_response(templates: list[dict]):
    mock = MagicMock()
    mock.status_code = 200
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"templates": templates, "count": len(templates)}
    return mock


def _make_create_response(template_id: int):
    mock = MagicMock()
    mock.status_code = 201
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"id": template_id}
    return mock


# ---------------------------------------------------------------------------
# Tests: naming convention
# ---------------------------------------------------------------------------


class TestBrevoNaming:

    def test_template_name_format(self, herbashop_it_config, mock_brevo_env):
        publisher = BrevoPublisher(herbashop_it_config)
        assert publisher._make_template_name(1, "colazione proteica") == "HS_IT_001_colazione_proteica"

    def test_sequence_zero_padded(self, herbashop_it_config, mock_brevo_env):
        publisher = BrevoPublisher(herbashop_it_config)
        assert publisher._make_template_name(27, "test") == "HS_IT_027_test"
        assert publisher._make_template_name(100, "test") == "HS_IT_100_test"

    def test_slug_truncated_at_40(self, herbashop_it_config, mock_brevo_env):
        publisher = BrevoPublisher(herbashop_it_config)
        long_slug = "questo e un argomento molto lungo che supera i quaranta caratteri totali"
        name = publisher._make_template_name(1, long_slug)
        slug_part = name.split("HS_IT_001_")[1]
        assert len(slug_part) <= 40

    def test_special_chars_replaced(self, herbashop_it_config, mock_brevo_env):
        publisher = BrevoPublisher(herbashop_it_config)
        name = publisher._make_template_name(1, "colazione: energia & salute!")
        assert ":" not in name
        assert "&" not in name
        assert "!" not in name


# ---------------------------------------------------------------------------
# Tests: sequence detection
# ---------------------------------------------------------------------------


class TestBrevoSequence:

    def test_next_sequence_no_existing_templates(self, herbashop_it_config, mock_brevo_env):
        publisher = BrevoPublisher(herbashop_it_config)
        empty_resp = _make_templates_response([])

        with patch("publishers.brevo.httpx.get", return_value=empty_resp):
            seq = publisher._get_next_sequence_number()

        assert seq == 1

    def test_next_sequence_after_existing(self, herbashop_it_config, mock_brevo_env):
        publisher = BrevoPublisher(herbashop_it_config)
        existing = [{"id": i, "name": f"HS_IT_{i:03d}_test"} for i in range(1, 6)]
        resp = _make_templates_response(existing)

        with patch("publishers.brevo.httpx.get", return_value=resp):
            seq = publisher._get_next_sequence_number()

        assert seq == 6


# ---------------------------------------------------------------------------
# Tests: publish_email_pair
# ---------------------------------------------------------------------------


class TestBrevoPublish:

    def test_publish_creates_two_templates(
        self, herbashop_it_config, sample_email_pair, mock_brevo_env
    ):
        publisher = BrevoPublisher(herbashop_it_config)
        empty_list = _make_templates_response([])
        create_1 = _make_create_response(101)
        create_2 = _make_create_response(102)

        with patch("publishers.brevo.httpx.get", return_value=empty_list), \
             patch("publishers.brevo.httpx.post") as mock_post, \
             patch("publishers.brevo.time.sleep"):

            mock_post.side_effect = [create_1, create_2]
            result = publisher.publish_email_pair(sample_email_pair, "colazione_proteica")

        assert isinstance(result, BrevoPublishResult)
        assert result.template_1_id == 101
        assert result.template_2_id == 102
        assert result.template_1_name == "HS_IT_001_colazione_proteica"
        assert result.template_2_name == "HS_IT_002_colazione_proteica"
        assert mock_post.call_count == 2

    def test_publish_skips_existing_templates(
        self, herbashop_it_config, sample_email_pair, mock_brevo_env
    ):
        """If templates already exist, POST is not called."""
        publisher = BrevoPublisher(herbashop_it_config)
        existing = [
            {"id": 101, "name": "HS_IT_001_colazione_proteica"},
            {"id": 102, "name": "HS_IT_002_colazione_proteica"},
        ]
        list_resp = _make_templates_response(existing)

        with patch("publishers.brevo.httpx.get", return_value=list_resp), \
             patch("publishers.brevo.httpx.post") as mock_post, \
             patch("publishers.brevo.time.sleep"):

            result = publisher.publish_email_pair(sample_email_pair, "colazione_proteica")

        assert result.template_1_id == 101
        assert result.template_2_id == 102
        assert mock_post.call_count == 0  # no creates

    def test_publish_correct_payload(
        self, herbashop_it_config, sample_email_pair, mock_brevo_env
    ):
        """Verify template payload includes sender, subject, templateName."""
        publisher = BrevoPublisher(herbashop_it_config)
        empty_list = _make_templates_response([])
        create_1 = _make_create_response(101)
        create_2 = _make_create_response(102)

        with patch("publishers.brevo.httpx.get", return_value=empty_list), \
             patch("publishers.brevo.httpx.post") as mock_post, \
             patch("publishers.brevo.time.sleep"):

            mock_post.side_effect = [create_1, create_2]
            publisher.publish_email_pair(sample_email_pair, "colazione_proteica")

        first_call_kwargs = mock_post.call_args_list[0].kwargs
        payload = first_call_kwargs["json"]
        assert payload["templateName"] == "HS_IT_001_colazione_proteica"
        assert payload["sender"]["email"] == "info@herbashop.it"
        assert payload["subject"] == sample_email_pair.email_1.subject
        assert payload["isActive"] is True

    def test_raises_without_api_key(self, herbashop_it_config):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="BREVO_API_KEY"):
                BrevoPublisher(herbashop_it_config)

    def test_raises_without_sender_email(self, herbashop_it_config, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.delenv("BREVO_SENDER_EMAIL", raising=False)
        with pytest.raises(EnvironmentError, match="BREVO_SENDER_EMAIL"):
            BrevoPublisher(herbashop_it_config)

    def test_raises_without_list_id(self, mock_brevo_env):
        cfg = SiteConfig(
            slug="test_brevo",
            url="https://test.com",
            language="it",
            locale="it-IT",
            platform="brevo",
            brevo_list_id=None,
        )
        with pytest.raises(EnvironmentError, match="brevo_list_id"):
            BrevoPublisher(cfg)
