"""
core/gsc_client.py

Google Search Console API client (webmasters v3 / searchanalytics).

Uses the same credentials as GA4:
  1. GOOGLE_CREDENTIALS_JSON env var (base64 JSON) — for Coolify
  2. File at GA4_CREDENTIALS_PATH — for local dev

All methods return empty lists on error — never crash the main process.
"""

import base64
import json
import os
from datetime import date
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


def _load_credentials():
    """Return google service account credentials or None on failure."""
    try:
        from google.oauth2 import service_account

        raw_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if raw_env:
            info = json.loads(base64.b64decode(raw_env).decode("utf-8"))
            log.info("gsc.credentials_loaded", source="env_var")
        else:
            path = os.getenv("GA4_CREDENTIALS_PATH", "./google_credentials.json")
            with open(path, "r", encoding="utf-8") as f:
                info = json.load(f)
            log.info("gsc.credentials_loaded", source="file", path=path)

        return service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    except Exception as exc:
        log.warning("gsc.credentials_error", error=str(exc))
        return None


class GSCClient:
    """
    Thin wrapper around the Search Console API for one property.

    property_url: "sc-domain:herbago.it"  (domain property format)
    """

    def __init__(self, property_url: str):
        self._property = property_url
        self._service = None
        self._unavailable_reason: Optional[str] = None

        if not property_url:
            self._unavailable_reason = "no property_url"
            return

        try:
            from googleapiclient.discovery import build

            creds = _load_credentials()
            if creds is None:
                self._unavailable_reason = "credentials not available"
                return
            self._service = build(
                "webmasters", "v3", credentials=creds, cache_discovery=False
            )
            log.info("gsc.client_ready", property=property_url)
        except Exception as exc:
            self._unavailable_reason = f"init error: {exc}"
            log.warning("gsc.client_init_error", error=str(exc))

    @property
    def available(self) -> bool:
        return self._service is not None

    @property
    def unavailable_reason(self) -> Optional[str]:
        return self._unavailable_reason

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _query(self, body: dict) -> dict:
        if not self.available:
            return {}
        try:
            return (
                self._service.searchanalytics()
                .query(siteUrl=self._property, body=body)
                .execute()
            )
        except Exception as exc:
            log.warning("gsc.query_error", error=str(exc), property=self._property)
            return {}

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_daily_rows(self, from_date: date, to_date: date) -> list[dict]:
        """
        Return daily clicks / impressions / ctr / position for the date range.
        Each entry: {date, clicks, impressions, ctr (0–1), position}
        """
        result = self._query({
            "startDate": from_date.isoformat(),
            "endDate": to_date.isoformat(),
            "dimensions": ["date"],
            "rowLimit": 500,
        })
        rows = []
        for row in result.get("rows", []):
            keys = row.get("keys", [])
            rows.append({
                "date": keys[0] if keys else None,
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": round(row.get("ctr", 0.0), 4),
                "position": round(row.get("position", 0.0), 1),
            })
        return rows

    def get_top_queries(
        self, from_date: date, to_date: date, limit: int = 25
    ) -> list[dict]:
        """
        Return top queries ordered by clicks.
        Each entry: {query, clicks, impressions, ctr (%), position}
        """
        result = self._query({
            "startDate": from_date.isoformat(),
            "endDate": to_date.isoformat(),
            "dimensions": ["query"],
            "rowLimit": limit,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        })
        rows = []
        for row in result.get("rows", []):
            keys = row.get("keys", [])
            rows.append({
                "query": keys[0] if keys else "",
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": round(row.get("ctr", 0.0) * 100, 2),
                "position": round(row.get("position", 0.0), 1),
            })
        return rows

    def get_top_pages(
        self, from_date: date, to_date: date, limit: int = 25
    ) -> list[dict]:
        """
        Return top pages ordered by clicks.
        Each entry: {page, clicks, impressions, ctr (%), position}
        """
        result = self._query({
            "startDate": from_date.isoformat(),
            "endDate": to_date.isoformat(),
            "dimensions": ["page"],
            "rowLimit": limit,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        })
        rows = []
        for row in result.get("rows", []):
            keys = row.get("keys", [])
            rows.append({
                "page": keys[0] if keys else "",
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": round(row.get("ctr", 0.0) * 100, 2),
                "position": round(row.get("position", 0.0), 1),
            })
        return rows
