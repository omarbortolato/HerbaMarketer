"""
publishers/wordpress.py

WordPress REST API client for HerbaMarketer.

Responsibilities:
- Create posts as drafts with content, slug, and Yoast SEO meta
- Upload featured images from a URL to the WP media library
- Update post status (draft → publish)

Authentication: HTTP Basic Auth with WordPress application passwords.
Credentials are resolved from environment variables referenced in sites.yaml
via SiteConfig.wp_user and SiteConfig.wp_password.

Public API:
    WordPressPublisher(site_config)
    publisher.publish_article(article, image_url=None, status="draft") -> WPPublishResult
    publisher.publish_post(post_id) -> None
"""

import time
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from agents.content_agent import ArticleOutput
from config import SiteConfig, get_settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class WPPublishResult:
    post_id: int
    post_url: str
    status: str   # "draft" | "publish"


# ---------------------------------------------------------------------------
# WordPressPublisher
# ---------------------------------------------------------------------------


class WordPressPublisher:
    """
    Publishes articles to WordPress for a specific site.

    Usage:
        publisher = WordPressPublisher(site_config)
        result = publisher.publish_article(article, image_url="https://...")
    """

    def __init__(self, site_config: SiteConfig) -> None:
        self.site = site_config
        self.api_url = (site_config.wp_api_url or "").rstrip("/")
        self.user = site_config.wp_user
        self.password = site_config.wp_password
        self.settings = get_settings()

        if not self.api_url:
            raise EnvironmentError(
                f"wp_api_url not configured for site {site_config.slug}"
            )
        if not self.user or not self.password:
            raise EnvironmentError(
                f"WordPress credentials missing for {site_config.slug} — "
                f"check {site_config.wp_user_env} / {site_config.wp_password_env} in .env"
            )

    def _auth(self) -> tuple[str, str]:
        return (self.user, self.password)

    def _delay(self) -> None:
        delay = self.settings.publishers.get("wordpress_delay_seconds", 1)
        time.sleep(delay)

    # ------------------------------------------------------------------
    # Media upload
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    def _upload_image_from_url(
        self,
        image_url: str,
        filename: str,
    ) -> Optional[int]:
        """
        Download an image from a URL and upload it to WP media library.
        Returns the WordPress media ID, or None if the download fails.
        """
        try:
            img_resp = httpx.get(image_url, timeout=30, follow_redirects=True)
            img_resp.raise_for_status()
        except Exception as exc:
            log.warning(
                "image_download_failed",
                site=self.site.slug,
                url=image_url,
                error=str(exc),
            )
            return None

        content_type = img_resp.headers.get("content-type", "image/jpeg")

        resp = httpx.post(
            f"{self.api_url}/media",
            auth=self._auth(),
            content=img_resp.content,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": content_type,
            },
            timeout=60,
            follow_redirects=True,
        )
        resp.raise_for_status()
        media_id = int(resp.json().get("id", 0)) or None
        log.info("wp_image_uploaded", site=self.site.slug, media_id=media_id)
        return media_id

    # ------------------------------------------------------------------
    # Post creation
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    def _create_post(self, payload: dict) -> dict:
        resp = httpx.post(
            f"{self.api_url}/posts",
            auth=self._auth(),
            json=payload,
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    def _update_post(self, post_id: int, payload: dict) -> dict:
        resp = httpx.post(
            f"{self.api_url}/posts/{post_id}",
            auth=self._auth(),
            json=payload,
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def publish_article(
        self,
        article: ArticleOutput,
        image_url: Optional[str] = None,
        status: str = "draft",
    ) -> WPPublishResult:
        """
        Create a WordPress post for the given article.

        Always creates as draft by default. Call publish_post() to go live.

        Args:
            article:   ArticleOutput from content_agent or translator_agent.
            image_url: Optional URL of an image to set as featured image.
            status:    "draft" (default) or "publish".

        Returns:
            WPPublishResult with post_id, post_url, and status.
        """
        log.info(
            "wp_publish_start",
            site=self.site.slug,
            title=article.title,
            status=status,
        )

        # Upload featured image if provided
        media_id = None
        if image_url:
            self._delay()
            filename = f"{article.slug or 'article'}-featured.jpg"
            media_id = self._upload_image_from_url(image_url, filename)

        # Build post payload
        # Yoast SEO fields: sent both as standard post meta and as top-level
        # fields that Yoast registers in the REST API (yoast_title / yoast_metadesc).
        # The underscore-prefixed keys (_yoast_wpseo_*) are the raw DB meta keys;
        # Yoast also exposes them without prefix via its REST schema.
        payload: dict = {
            "title": article.title,
            "content": article.content_html,
            "slug": article.slug,
            "status": status,
            "meta": {
                "_yoast_wpseo_title": article.meta_title,
                "_yoast_wpseo_metadesc": article.meta_description,
                "yoast_wpseo_title": article.meta_title,
                "yoast_wpseo_metadesc": article.meta_description,
            },
        }
        if media_id:
            payload["featured_media"] = media_id

        self._delay()
        data = self._create_post(payload)

        post_id = int(data.get("id", 0))
        post_url = data.get("link", "")

        log.info(
            "wp_post_created",
            site=self.site.slug,
            post_id=post_id,
            url=post_url,
            status=status,
        )

        return WPPublishResult(
            post_id=post_id,
            post_url=post_url,
            status=status,
        )

    def publish_post(self, post_id: int) -> None:
        """
        Change a draft post to published status.

        Args:
            post_id: WordPress post ID to publish.
        """
        self._delay()
        self._update_post(post_id, {"status": "publish"})
        log.info("wp_post_published", site=self.site.slug, post_id=post_id)
