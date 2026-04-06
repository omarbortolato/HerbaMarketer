"""
tests/test_ga4_client.py

Unit tests for core/ga4_client.py.
All GA4 API calls are mocked — no real network calls.
"""

import base64
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from core.ga4_client import GA4Client, _load_credentials


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _fake_row(dim_values: list[str], metric_values: list[str]):
    """Build a mock GA4 report row."""
    row = MagicMock()
    row.dimension_values = [MagicMock(value=v) for v in dim_values]
    row.metric_values = [MagicMock(value=v) for v in metric_values]
    return row


def _fake_response(rows):
    resp = MagicMock()
    resp.rows = rows
    return resp


FAKE_CREDS_DICT = {
    "type": "service_account",
    "project_id": "test-project",
    "private_key_id": "key-id",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xHn/ygWep4PAtE\n-----END RSA PRIVATE KEY-----\n",
    "client_email": "test@test-project.iam.gserviceaccount.com",
    "client_id": "123456",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


# ---------------------------------------------------------------------------
# Credentials loading
# ---------------------------------------------------------------------------


class TestLoadCredentials:
    def test_loads_from_env_var(self, monkeypatch):
        """Should base64-decode GOOGLE_CREDENTIALS_JSON and return credentials."""
        encoded = base64.b64encode(json.dumps(FAKE_CREDS_DICT).encode()).decode()
        monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", encoded)

        with (
            patch("core.ga4_client.service_account") as mock_sa,
        ):
            mock_sa.Credentials.from_service_account_info.return_value = MagicMock()
            creds = _load_credentials()
            mock_sa.Credentials.from_service_account_info.assert_called_once()
            call_args = mock_sa.Credentials.from_service_account_info.call_args
            assert call_args[0][0]["project_id"] == "test-project"

    def test_falls_back_to_file(self, monkeypatch, tmp_path):
        """Should read from file when env var is not set."""
        creds_file = tmp_path / "google_credentials.json"
        creds_file.write_text(json.dumps(FAKE_CREDS_DICT))
        monkeypatch.delenv("GOOGLE_CREDENTIALS_JSON", raising=False)
        monkeypatch.setenv("GA4_CREDENTIALS_PATH", str(creds_file))

        with patch("core.ga4_client.service_account") as mock_sa:
            mock_sa.Credentials.from_service_account_info.return_value = MagicMock()
            creds = _load_credentials()
            mock_sa.Credentials.from_service_account_info.assert_called_once()

    def test_returns_none_on_missing_file(self, monkeypatch):
        """Should return None gracefully when credentials file doesn't exist."""
        monkeypatch.delenv("GOOGLE_CREDENTIALS_JSON", raising=False)
        monkeypatch.setenv("GA4_CREDENTIALS_PATH", "/nonexistent/path/creds.json")
        creds = _load_credentials()
        assert creds is None


# ---------------------------------------------------------------------------
# GA4Client — get_site_overview
# ---------------------------------------------------------------------------


class TestGetSiteOverview:
    def _make_client(self, mock_beta_client):
        """Return a GA4Client with a mocked BetaAnalyticsDataClient."""
        with (
            patch("core.ga4_client._load_credentials", return_value=MagicMock()),
            patch("core.ga4_client.BetaAnalyticsDataClient", return_value=mock_beta_client),
        ):
            client = GA4Client("123456789")
        return client

    def test_returns_parsed_overview(self):
        mock_api = MagicMock()
        row = _fake_row(
            dim_values=[],
            metric_values=["1200", "800", "300", "0.65", "120.5", "3500"],
        )
        mock_api.run_report.return_value = _fake_response([row])

        with (
            patch("core.ga4_client._load_credentials", return_value=MagicMock()),
            patch("core.ga4_client.BetaAnalyticsDataClient", return_value=mock_api),
        ):
            client = GA4Client("123456789")

        result = client.get_site_overview(period_days=30)
        assert result is not None
        assert result["sessions"] == 1200
        assert result["total_users"] == 800
        assert result["new_users"] == 300
        assert abs(result["engagement_rate"] - 0.65) < 0.001
        assert result["pageviews"] == 3500

    def test_returns_none_on_empty_response(self):
        mock_api = MagicMock()
        mock_api.run_report.return_value = _fake_response([])

        with (
            patch("core.ga4_client._load_credentials", return_value=MagicMock()),
            patch("core.ga4_client.BetaAnalyticsDataClient", return_value=mock_api),
        ):
            client = GA4Client("123456789")

        result = client.get_site_overview()
        assert result is None

    def test_returns_none_on_api_error(self):
        mock_api = MagicMock()
        mock_api.run_report.side_effect = Exception("API error")

        with (
            patch("core.ga4_client._load_credentials", return_value=MagicMock()),
            patch("core.ga4_client.BetaAnalyticsDataClient", return_value=mock_api),
        ):
            client = GA4Client("123456789")

        result = client.get_site_overview()
        assert result is None  # must not propagate


# ---------------------------------------------------------------------------
# GA4Client — get_ecommerce_overview (cart_abandonment_rate)
# ---------------------------------------------------------------------------


class TestGetEcommerceOverview:
    def test_cart_abandonment_rate_calculation(self):
        """cart_abandonment_rate = 1 - (checkouts / add_to_carts)"""
        mock_api = MagicMock()
        # purchases=50, revenue=2500, add_to_carts=200, checkouts=80
        row = _fake_row(dim_values=[], metric_values=["50", "2500.00", "200", "80"])
        mock_api.run_report.return_value = _fake_response([row])

        with (
            patch("core.ga4_client._load_credentials", return_value=MagicMock()),
            patch("core.ga4_client.BetaAnalyticsDataClient", return_value=mock_api),
        ):
            client = GA4Client("123456789")

        result = client.get_ecommerce_overview()
        assert result is not None
        assert result["purchases"] == 50
        assert result["revenue"] == 2500.0
        assert result["avg_order_value"] == 50.0  # 2500/50
        assert result["add_to_carts"] == 200
        assert result["checkouts"] == 80
        # 1 - (80/200) = 0.6
        assert abs(result["cart_abandonment_rate"] - 0.6) < 0.001

    def test_cart_abandonment_zero_add_to_carts(self):
        """Should return 0.0 abandonment rate when add_to_carts is 0."""
        mock_api = MagicMock()
        row = _fake_row(dim_values=[], metric_values=["0", "0", "0", "0"])
        mock_api.run_report.return_value = _fake_response([row])

        with (
            patch("core.ga4_client._load_credentials", return_value=MagicMock()),
            patch("core.ga4_client.BetaAnalyticsDataClient", return_value=mock_api),
        ):
            client = GA4Client("123456789")

        result = client.get_ecommerce_overview()
        assert result is not None
        assert result["cart_abandonment_rate"] == 0.0


# ---------------------------------------------------------------------------
# GA4Client — skip when no property_id
# ---------------------------------------------------------------------------


class TestSkipNoPropertyId:
    def test_not_available_when_no_property_id(self):
        client = GA4Client("")
        assert not client.available

    def test_not_available_when_placeholder(self):
        client = GA4Client("DA_AGGIUNGERE")
        assert not client.available

    def test_get_site_overview_returns_none_when_not_available(self):
        client = GA4Client("DA_AGGIUNGERE")
        assert client.get_site_overview() is None

    def test_get_top_pages_returns_empty_when_not_available(self):
        client = GA4Client("")
        assert client.get_top_pages() == []
