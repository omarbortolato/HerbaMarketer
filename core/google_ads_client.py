"""
core/google_ads_client.py

Google Ads API client wrapper.

Auth credentials (all required, set in .env):
  GOOGLE_ADS_DEVELOPER_TOKEN   — from My API Center in the Google Ads account
  GOOGLE_ADS_CLIENT_ID         — OAuth2 client ID (Google Cloud Console)
  GOOGLE_ADS_CLIENT_SECRET     — OAuth2 client secret
  GOOGLE_ADS_REFRESH_TOKEN     — long-lived OAuth2 refresh token

Customer ID is passed per-request (one per site, no MCC).

All methods return empty structures on error — never crash the main process.
"""

import os
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

_PERIOD_MAP = {
    7:  "LAST_7_DAYS",
    30: "LAST_30_DAYS",
    90: "LAST_90_DAYS",
}


def _build_config() -> Optional[dict]:
    """Build the google-ads config dict from env vars. Returns None if any key is missing."""
    dev_token = (os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN") or "").strip()
    client_id = (os.getenv("GOOGLE_ADS_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_ADS_CLIENT_SECRET") or "").strip()
    refresh_token = (os.getenv("GOOGLE_ADS_REFRESH_TOKEN") or "").strip()
    login_customer_id = (os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or "").strip()

    log.debug(
        "google_ads.credentials_lengths",
        developer_token_len=len(dev_token),
        client_id_len=len(client_id),
        client_secret_len=len(client_secret),
        refresh_token_len=len(refresh_token),
        login_customer_id=login_customer_id or "NOT_SET",
        client_id_suffix=client_id[-20:] if client_id else "",
    )

    if not all([dev_token, client_id, client_secret, refresh_token]):
        missing = [
            k for k, v in {
                "GOOGLE_ADS_DEVELOPER_TOKEN": dev_token,
                "GOOGLE_ADS_CLIENT_ID": client_id,
                "GOOGLE_ADS_CLIENT_SECRET": client_secret,
                "GOOGLE_ADS_REFRESH_TOKEN": refresh_token,
            }.items() if not v
        ]
        log.warning("google_ads.missing_credentials", missing=missing)
        return None

    config = {
        "developer_token": dev_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    if login_customer_id:
        config["login_customer_id"] = login_customer_id
    return config


class GoogleAdsClient:
    """
    Thin wrapper around the Google Ads API for one customer account.

    customer_id: numeric string without dashes, e.g. "7708381052"
    """

    def __init__(self, customer_id: str):
        self._customer_id = customer_id
        self._client = None
        self._unavailable_reason: Optional[str] = None

        if not customer_id:
            self._unavailable_reason = "no customer_id"
            log.debug("google_ads.client_skipped", reason="no customer_id")
            return

        env_vars = {
            "GOOGLE_ADS_DEVELOPER_TOKEN": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
            "GOOGLE_ADS_CLIENT_ID": os.getenv("GOOGLE_ADS_CLIENT_ID"),
            "GOOGLE_ADS_CLIENT_SECRET": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
            "GOOGLE_ADS_REFRESH_TOKEN": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
        }
        log.debug(
            "google_ads.env_vars_check",
            customer_id=customer_id,
            **{k: ("SET" if v else "MISSING") for k, v in env_vars.items()},
        )

        config = _build_config()
        if config is None:
            missing = [k for k, v in env_vars.items() if not v]
            self._unavailable_reason = f"missing env vars: {', '.join(missing)}"
            return

        try:
            from google.ads.googleads.client import GoogleAdsClient as _GAC
            self._client = _GAC.load_from_dict(config, version="v17")
            log.info("google_ads.client_ready", customer_id=customer_id)
        except Exception as exc:
            self._unavailable_reason = f"init error: {exc}"
            log.warning("google_ads.client_init_error", error=str(exc))

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> Optional[str]:
        return self._unavailable_reason

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_query(self, gaql: str) -> list:
        """Execute a GAQL query; return list of rows or [] on error."""
        if not self.available:
            return []
        try:
            service = self._client.get_service("GoogleAdsService")
            response = service.search(customer_id=self._customer_id, query=gaql)
            rows = list(response)
            log.warning(
                "google_ads.DEBUG_query_result",
                customer_id=self._customer_id,
                login_customer_id=(os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or "NOT_SET").strip(),
                rows_returned=len(rows),
                raw_response=str(response) if not rows else "non-empty",
            )
            return rows
        except Exception as exc:
            import traceback
            log.warning(
                "google_ads.query_error",
                error=str(exc),
                error_type=type(exc).__name__,
                traceback=traceback.format_exc(),
                customer=self._customer_id,
            )
            return []

    @staticmethod
    def _micros_to_eur(micros) -> float:
        """Convert cost_micros to currency (divide by 1_000_000)."""
        try:
            return float(micros) / 1_000_000
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _roas(conversions_value: float, cost: float) -> Optional[float]:
        if cost > 0:
            return round(conversions_value / cost, 2)
        return None

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_account_overview(self, period_days: int = 30) -> Optional[dict]:
        """
        Return account-level KPIs for the given period.

        Returns:
            {impressions, clicks, ctr, cost, conversions, conversions_value, roas}
        or None on error / no data.
        """
        date_range = _PERIOD_MAP.get(period_days, "LAST_30_DAYS")
        gaql = f"""
            SELECT
              metrics.impressions,
              metrics.clicks,
              metrics.ctr,
              metrics.cost_micros,
              metrics.conversions,
              metrics.conversions_value
            FROM customer
            WHERE segments.date DURING {date_range}
        """
        rows = self._run_query(gaql)
        if not rows:
            return None
        try:
            row = rows[0]
            m = row.metrics
            cost = self._micros_to_eur(m.cost_micros)
            conv_value = float(m.conversions_value)
            return {
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "ctr": round(float(m.ctr), 4),
                "cost": round(cost, 2),
                "conversions": round(float(m.conversions), 1),
                "conversions_value": round(conv_value, 2),
                "roas": self._roas(conv_value, cost),
            }
        except Exception as exc:
            log.warning("google_ads.parse_account_error", error=str(exc))
            return None

    def get_campaigns(self, period_days: int = 30) -> list[dict]:
        """
        Return per-campaign KPIs for the given period, ordered by cost descending.

        Each entry:
            {campaign_id, campaign_name, status,
             impressions, clicks, ctr, cost, conversions, conversions_value, roas}
        """
        date_range = _PERIOD_MAP.get(period_days, "LAST_30_DAYS")
        gaql = f"""
            SELECT
              campaign.id,
              campaign.name,
              campaign.status,
              campaign.advertising_channel_type,
              metrics.clicks,
              metrics.cost_micros,
              metrics.conversions_value,
              metrics.conversions,
              metrics.impressions
            FROM campaign
            WHERE segments.date DURING {date_range}
              AND campaign.status != 'REMOVED'
        """
        log.warning(
            "google_ads.DEBUG_get_campaigns_called",
            customer_id=self._customer_id,
            login_customer_id=(os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or "NOT_SET").strip(),
            period_days=period_days,
        )
        rows = self._run_query(gaql)
        results = []
        for row in rows:
            try:
                m = row.metrics
                cost = self._micros_to_eur(m.cost_micros)
                conv_value = float(m.conversions_value)
                # Skip campaigns with zero spend and zero impressions
                if int(m.impressions) == 0 and cost == 0:
                    continue
                results.append({
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "status": row.campaign.status.name,  # ENABLED / PAUSED
                    "advertising_channel_type": row.campaign.advertising_channel_type.name,
                    "impressions": int(m.impressions),
                    "clicks": int(m.clicks),
                    "cost": round(cost, 2),
                    "conversions": round(float(m.conversions), 1),
                    "conversions_value": round(conv_value, 2),
                    "roas": self._roas(conv_value, cost),
                })
            except Exception as exc:
                log.warning("google_ads.parse_campaign_row_error", error=str(exc))
        return results
