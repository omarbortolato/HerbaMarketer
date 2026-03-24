"""
tests/test_fase2.py

Unit tests for Fase 2 components:
  - agents/seo_agent.py
  - inputs/url_ingestor.py
  - inputs/email_ingestor.py
  - publishers/wordpress.py
  - core/image_generator.py

All HTTP calls and DB sessions are mocked.
Run with: pytest tests/test_fase2.py -v
"""

import os
from unittest.mock import MagicMock, patch, call

import pytest

from config import SiteConfig


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
        wp_api_url="https://herbago.it/wp-json/wp/v2",
        wp_user_env="WP_HERBAGO_IT_USER",
        wp_password_env="WP_HERBAGO_IT_APP_PASSWORD",
        preferred_customer_url="https://www.hlifeclienteprivilegiato.it/",
        distributor_url="https://www.hl-distributor.com/it/",
        active=True,
    )


@pytest.fixture
def mock_wp_env(monkeypatch):
    monkeypatch.setenv("WP_HERBAGO_IT_USER", "admin")
    monkeypatch.setenv("WP_HERBAGO_IT_APP_PASSWORD", "xxxx yyyy zzzz")


@pytest.fixture
def mock_dataforseo_env(monkeypatch):
    monkeypatch.setenv("DATAFORSEO_LOGIN", "test@example.com")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "testpassword")


# ---------------------------------------------------------------------------
# seo_agent tests
# ---------------------------------------------------------------------------


class TestSeoAgent:

    def _make_dataforseo_response(self, keywords: list[dict]):
        mock = MagicMock()
        mock.status_code = 200
        mock.raise_for_status = MagicMock()
        mock.json.return_value = {
            "tasks": [
                {
                    "status_code": 20000,
                    "result": [
                        {
                            "items": [
                                {
                                    "keyword_data": {
                                        "keyword": kw["keyword"],
                                        "keyword_info": {
                                            "search_volume": kw["volume"],
                                            "competition": 0.5,
                                            "cpc": 0.8,
                                            "monthly_searches": [
                                                {"search_volume": kw["volume"]}
                                            ] * 12,
                                        },
                                    }
                                }
                                for kw in keywords
                            ]
                        }
                    ],
                }
            ]
        }
        return mock

    def test_research_keywords_returns_sorted_results(
        self, herbago_it_config, mock_dataforseo_env
    ):
        from agents.seo_agent import research_keywords

        mock_resp = self._make_dataforseo_response([
            {"keyword": "colazione proteica", "volume": 1000},
            {"keyword": "shake herbalife", "volume": 5000},
            {"keyword": "dieta sana", "volume": 2500},
        ])

        with patch("agents.seo_agent.httpx.post", return_value=mock_resp):
            results = research_keywords("herbalife", herbago_it_config)

        assert len(results) == 3
        # Sorted by search_volume descending
        assert results[0].keyword == "shake herbalife"
        assert results[0].search_volume == 5000
        assert results[1].search_volume == 2500
        assert results[2].search_volume == 1000

    def test_research_keywords_filters_by_min_volume(
        self, herbago_it_config, mock_dataforseo_env
    ):
        from agents.seo_agent import research_keywords

        mock_resp = self._make_dataforseo_response([
            {"keyword": "alta volume", "volume": 500},
            {"keyword": "bassa volume", "volume": 50},  # below min_volume=100
        ])

        with patch("agents.seo_agent.httpx.post", return_value=mock_resp):
            results = research_keywords("herbalife", herbago_it_config, min_volume=100)

        assert len(results) == 1
        assert results[0].keyword == "alta volume"

    def test_propose_topics_returns_capitalized(
        self, herbago_it_config, mock_dataforseo_env
    ):
        from agents.seo_agent import propose_topics

        mock_resp = self._make_dataforseo_response([
            {"keyword": "colazione proteica herbalife", "volume": 1000},
            {"keyword": "shake mattino", "volume": 800},
        ])

        with patch("agents.seo_agent.httpx.post", return_value=mock_resp):
            topics = propose_topics("herbalife", herbago_it_config, max_topics=2)

        assert len(topics) == 2
        assert topics[0][0].isupper()  # first char capitalized

    def test_propose_topics_empty_when_no_results(
        self, herbago_it_config, mock_dataforseo_env
    ):
        from agents.seo_agent import propose_topics

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "tasks": [{"status_code": 20000, "result": [{"items": []}]}]
        }

        with patch("agents.seo_agent.httpx.post", return_value=mock_resp):
            topics = propose_topics("unknown_seed", herbago_it_config)

        assert topics == []

    def test_raises_without_credentials(self, herbago_it_config):
        from agents.seo_agent import research_keywords

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="DATAFORSEO"):
                research_keywords("test", herbago_it_config)


# ---------------------------------------------------------------------------
# url_ingestor tests
# ---------------------------------------------------------------------------


class TestUrlIngestor:

    def _make_scrape_response(self, html: str):
        mock = MagicMock()
        mock.status_code = 200
        mock.raise_for_status = MagicMock()
        mock.text = html
        mock.headers = {"content-type": "text/html"}
        return mock

    def test_ingest_url_creates_topic(self):
        from inputs.url_ingestor import ingest_url

        html = "<html><body><article><p>Articolo sulla colazione sana con Herbalife</p></article></body></html>"
        scrape_resp = self._make_scrape_response(html)
        claude_result = {"title": "Colazione sana con Herbalife", "keyword": "colazione proteica"}

        mock_db = MagicMock()
        mock_db.add = MagicMock()
        mock_db.commit = MagicMock()
        mock_db.refresh = MagicMock()

        with patch("inputs.url_ingestor.httpx.get", return_value=scrape_resp), \
             patch("inputs.url_ingestor._call_claude", return_value=claude_result):
            result = ingest_url("https://example.com/article", mock_db)

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called()

    def test_ingest_url_returns_none_on_scrape_failure(self):
        from inputs.url_ingestor import ingest_url

        mock_db = MagicMock()

        with patch("inputs.url_ingestor.httpx.get", side_effect=Exception("timeout")):
            result = ingest_url("https://example.com/article", mock_db)

        assert result is None
        mock_db.add.assert_not_called()

    def test_ingest_url_returns_none_on_empty_content(self):
        from inputs.url_ingestor import ingest_url

        scrape_resp = self._make_scrape_response("<html><body></body></html>")
        mock_db = MagicMock()

        with patch("inputs.url_ingestor.httpx.get", return_value=scrape_resp):
            result = ingest_url("https://example.com/empty", mock_db)

        assert result is None


# ---------------------------------------------------------------------------
# email_ingestor tests
# ---------------------------------------------------------------------------


class TestEmailIngestor:

    def test_raises_without_credentials(self):
        from inputs.email_ingestor import run_email_ingestor

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="INGESTOR_EMAIL"):
                run_email_ingestor(MagicMock())

    def test_run_email_ingestor_no_unread(self, monkeypatch):
        from inputs.email_ingestor import run_email_ingestor

        monkeypatch.setenv("INGESTOR_EMAIL", "test@gmail.com")
        monkeypatch.setenv("INGESTOR_PASSWORD", "app_password")

        mock_imap = MagicMock()
        mock_imap.search.return_value = ("OK", [b""])  # no unread
        mock_imap.login.return_value = ("OK", [b""])
        mock_imap.select.return_value = ("OK", [b"1"])

        with patch("inputs.email_ingestor.imaplib.IMAP4_SSL", return_value=mock_imap):
            result = run_email_ingestor(MagicMock())

        assert result == []

    def test_run_email_ingestor_creates_topics(self, monkeypatch):
        import email as email_lib

        from inputs.email_ingestor import run_email_ingestor

        monkeypatch.setenv("INGESTOR_EMAIL", "test@gmail.com")
        monkeypatch.setenv("INGESTOR_PASSWORD", "app_password")

        # Build a minimal RFC822 email
        msg = email_lib.message.Message()
        msg["Subject"] = "Benefici della colazione"
        msg.set_payload("Testo sull'importanza della colazione proteica.", charset="utf-8")
        raw_bytes = msg.as_bytes()

        mock_imap = MagicMock()
        mock_imap.login.return_value = ("OK", [b""])
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.search.return_value = ("OK", [b"1"])
        mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {...})", raw_bytes)])
        mock_imap.store.return_value = ("OK", [b""])
        mock_imap.logout.return_value = ("OK", [b""])

        mock_db = MagicMock()
        mock_db.add = MagicMock()
        mock_db.commit = MagicMock()
        mock_db.refresh = MagicMock()

        claude_result = {"title": "Importanza della colazione", "keyword": "colazione proteica"}

        with patch("inputs.email_ingestor.imaplib.IMAP4_SSL", return_value=mock_imap), \
             patch("inputs.email_ingestor._call_claude", return_value=claude_result):
            result = run_email_ingestor(mock_db)

        assert len(result) == 1
        mock_db.add.assert_called_once()


# ---------------------------------------------------------------------------
# wordpress publisher tests
# ---------------------------------------------------------------------------


class TestWordPressPublisher:

    def _make_wp_post_response(self, post_id: int, url: str):
        mock = MagicMock()
        mock.status_code = 201
        mock.raise_for_status = MagicMock()
        mock.json.return_value = {"id": post_id, "link": url, "status": "draft"}
        return mock

    def _make_wp_media_response(self, media_id: int):
        mock = MagicMock()
        mock.status_code = 201
        mock.raise_for_status = MagicMock()
        mock.json.return_value = {"id": media_id}
        return mock

    def _sample_article(self):
        from agents.content_agent import ArticleOutput
        return ArticleOutput(
            title="Colazione proteica con Herbalife",
            slug="colazione-proteica-herbalife",
            content_html="<h3>Intro</h3><p>Testo articolo...</p>",
            meta_title="Colazione proteica — Herbalife",
            meta_description="Scopri come la colazione proteica migliora la tua giornata.",
            image_prompt="Wellness breakfast scene with fruits and shakes",
            language="it",
            site_slug="herbago_it",
        )

    def test_publish_article_creates_draft(self, herbago_it_config, mock_wp_env):
        from publishers.wordpress import WordPressPublisher

        post_resp = self._make_wp_post_response(42, "https://herbago.it/?p=42")
        article = self._sample_article()

        with patch("publishers.wordpress.httpx.post", return_value=post_resp), \
             patch("publishers.wordpress.time.sleep"):
            publisher = WordPressPublisher(herbago_it_config)
            result = publisher.publish_article(article)

        assert result.post_id == 42
        assert result.status == "draft"

    def test_publish_article_with_image(self, herbago_it_config, mock_wp_env):
        from publishers.wordpress import WordPressPublisher

        media_resp = self._make_wp_media_response(99)
        post_resp = self._make_wp_post_response(43, "https://herbago.it/?p=43")
        article = self._sample_article()

        # Mock image download
        img_resp = MagicMock()
        img_resp.raise_for_status = MagicMock()
        img_resp.content = b"fake_image_bytes"
        img_resp.headers = {"content-type": "image/jpeg"}

        with patch("publishers.wordpress.httpx.get", return_value=img_resp), \
             patch("publishers.wordpress.httpx.post") as mock_post, \
             patch("publishers.wordpress.time.sleep"):

            mock_post.side_effect = [media_resp, post_resp]
            publisher = WordPressPublisher(herbago_it_config)
            result = publisher.publish_article(
                article, image_url="https://cdn.example.com/image.jpg"
            )

        assert result.post_id == 43
        # Two POST calls: media upload + post create
        assert mock_post.call_count == 2

    def test_publish_article_skips_image_on_download_failure(
        self, herbago_it_config, mock_wp_env
    ):
        from publishers.wordpress import WordPressPublisher

        post_resp = self._make_wp_post_response(44, "https://herbago.it/?p=44")
        article = self._sample_article()

        with patch("publishers.wordpress.httpx.get", side_effect=Exception("timeout")), \
             patch("publishers.wordpress.httpx.post", return_value=post_resp), \
             patch("publishers.wordpress.time.sleep"):
            publisher = WordPressPublisher(herbago_it_config)
            # Should not raise — just skips the image
            result = publisher.publish_article(
                article, image_url="https://cdn.example.com/image.jpg"
            )

        assert result.post_id == 44

    def test_publish_post_calls_update(self, herbago_it_config, mock_wp_env):
        from publishers.wordpress import WordPressPublisher

        update_resp = MagicMock()
        update_resp.raise_for_status = MagicMock()
        update_resp.json.return_value = {"id": 42, "status": "publish"}

        with patch("publishers.wordpress.httpx.post", return_value=update_resp), \
             patch("publishers.wordpress.time.sleep"):
            publisher = WordPressPublisher(herbago_it_config)
            publisher.publish_post(42)

        update_resp.raise_for_status.assert_called()

    def test_raises_on_missing_wp_credentials(self, herbago_it_config):
        from publishers.wordpress import WordPressPublisher

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="credentials"):
                WordPressPublisher(herbago_it_config)

    def test_raises_on_missing_wp_api_url(self):
        from publishers.wordpress import WordPressPublisher

        cfg = SiteConfig(
            slug="test_site",
            url="https://test.com",
            language="it",
            locale="it-IT",
            platform="mautic",
        )
        with pytest.raises(EnvironmentError, match="wp_api_url"):
            WordPressPublisher(cfg)


# ---------------------------------------------------------------------------
# image_generator tests
# ---------------------------------------------------------------------------


class TestImageGenerator:

    def test_uses_dalle3_when_openai_key_set(self, monkeypatch):
        from core.image_generator import generate_image

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("IDEOGRAM_API_KEY", raising=False)

        with patch("core.image_generator._generate_dalle3", return_value="https://dalle.url/img.jpg") as mock_d:
            url = generate_image("a wellness scene")

        mock_d.assert_called_once_with("a wellness scene")
        assert url == "https://dalle.url/img.jpg"

    def test_uses_ideogram_as_fallback(self, monkeypatch):
        from core.image_generator import generate_image

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("IDEOGRAM_API_KEY", "ideogram-test-key")

        with patch("core.image_generator._generate_ideogram", return_value="https://ideogram.url/img.jpg") as mock_i:
            url = generate_image("a wellness scene")

        mock_i.assert_called_once_with("a wellness scene")
        assert url == "https://ideogram.url/img.jpg"

    def test_raises_when_no_provider_configured(self, monkeypatch):
        from core.image_generator import generate_image

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("IDEOGRAM_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="No image generation"):
            generate_image("a wellness scene")
