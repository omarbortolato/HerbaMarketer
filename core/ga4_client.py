"""
core/ga4_client.py

Google Analytics 4 Data API client.

Credential loading priority:
  1. GOOGLE_CREDENTIALS_JSON env var (base64-encoded JSON) — for Coolify deploys
  2. File at GA4_CREDENTIALS_PATH (default: ./google_credentials.json) — for local dev

All methods return None / empty structures on error (never crash the main process).
"""

import base64
import json
import os
from datetime import date
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


def _load_credentials():
    """Return google.oauth2.service_account.Credentials or None on failure."""
    try:
        from google.oauth2 import service_account

        raw_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if raw_env:
            creds_dict = json.loads(base64.b64decode(raw_env).decode("utf-8"))
            log.info("ga4.credentials_loaded", source="env_var")
        else:
            creds_path = os.getenv("GA4_CREDENTIALS_PATH", "./google_credentials.json")
            with open(creds_path, "r", encoding="utf-8") as f:
                creds_dict = json.load(f)
            log.info("ga4.credentials_loaded", source="file", path=creds_path)

        scopes = ["https://www.googleapis.com/auth/analytics.readonly"]
        return service_account.Credentials.from_service_account_info(creds_dict, scopes=scopes)
    except Exception as exc:
        log.warning("ga4.credentials_error", error=str(exc))
        return None


class GA4Client:
    """Thin wrapper around the GA4 Data API v1beta."""

    def __init__(self, property_id: str):
        """
        property_id: numeric GA4 property ID (e.g. "489961908")
        Skips initialisation if property_id is missing or a placeholder.
        """
        self._property = f"properties/{property_id}"
        self._client = None

        if not property_id or property_id == "DA_AGGIUNGERE":
            log.debug("ga4.client_skipped", property_id=property_id)
            return

        credentials = _load_credentials()
        if credentials is None:
            return

        try:
            from google.analytics.data_v1beta import BetaAnalyticsDataClient

            self._client = BetaAnalyticsDataClient(credentials=credentials)
            log.info("ga4.client_ready", property=self._property)
        except Exception as exc:
            log.warning("ga4.client_init_error", error=str(exc))

    @property
    def available(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_report(self, date_ranges, dimensions, metrics) -> Optional[object]:
        """Execute a runReport call; return the response or None on error."""
        if not self.available:
            return None
        try:
            from google.analytics.data_v1beta.types import (
                DateRange,
                Dimension,
                Metric,
                RunReportRequest,
            )

            request = RunReportRequest(
                property=self._property,
                date_ranges=[DateRange(**dr) for dr in date_ranges],
                dimensions=[Dimension(name=d) for d in dimensions],
                metrics=[Metric(name=m) for m in metrics],
            )
            return self._client.run_report(request)
        except Exception as exc:
            log.warning("ga4.run_report_error", error=str(exc))
            return None

    @staticmethod
    def _row_value(row, index: int) -> str:
        try:
            return row.dimension_values[index].value
        except (IndexError, AttributeError):
            return ""

    @staticmethod
    def _metric_value(row, index: int) -> str:
        try:
            return row.metric_values[index].value
        except (IndexError, AttributeError):
            return "0"

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_site_overview(self, period_days: int = 30) -> Optional[dict]:
        """
        Return high-level site traffic metrics for the last `period_days` days.

        Returns:
            {sessions, total_users, new_users, engagement_rate,
             avg_session_duration, pageviews}
        or None on error.
        """
        end = "today"
        start = f"{period_days}daysAgo"
        response = self._run_report(
            date_ranges=[{"start_date": start, "end_date": end}],
            dimensions=[],
            metrics=[
                "sessions",
                "totalUsers",
                "newUsers",
                "engagementRate",
                "averageSessionDuration",
                "screenPageViews",
            ],
        )
        if response is None or not response.rows:
            return None
        try:
            row = response.rows[0]
            mv = lambda i: self._metric_value(row, i)
            return {
                "sessions": int(mv(0)),
                "total_users": int(mv(1)),
                "new_users": int(mv(2)),
                "engagement_rate": float(mv(3)),
                "avg_session_duration": float(mv(4)),
                "pageviews": int(mv(5)),
            }
        except Exception as exc:
            log.warning("ga4.parse_overview_error", error=str(exc))
            return None

    def get_ecommerce_overview(self, period_days: int = 30) -> Optional[dict]:
        """
        Return ecommerce KPIs for the last `period_days` days.

        Returns:
            {purchases, revenue, avg_order_value,
             add_to_carts, checkouts, cart_abandonment_rate}
        or None on error.
        """
        end = "today"
        start = f"{period_days}daysAgo"
        response = self._run_report(
            date_ranges=[{"start_date": start, "end_date": end}],
            dimensions=[],
            metrics=[
                "transactions",
                "purchaseRevenue",
                "addToCarts",
                "checkouts",
            ],
        )
        if response is None or not response.rows:
            return None
        try:
            row = response.rows[0]
            mv = lambda i: self._metric_value(row, i)
            purchases = int(mv(0))
            revenue = float(mv(1))
            add_to_carts = int(mv(2))
            checkouts = int(mv(3))

            avg_order_value = revenue / purchases if purchases > 0 else 0.0
            # cart_abandonment_rate: share of add-to-cart that never reached checkout
            if add_to_carts > 0:
                cart_abandonment_rate = 1.0 - (checkouts / add_to_carts)
            else:
                cart_abandonment_rate = 0.0

            return {
                "purchases": purchases,
                "revenue": round(revenue, 2),
                "avg_order_value": round(avg_order_value, 2),
                "add_to_carts": add_to_carts,
                "checkouts": checkouts,
                "cart_abandonment_rate": round(cart_abandonment_rate, 4),
            }
        except Exception as exc:
            log.warning("ga4.parse_ecommerce_error", error=str(exc))
            return None

    def get_returning_customers(self, period_days: int = 30) -> Optional[float]:
        """
        Return returning customer rate (purchases by returning users / total purchases).
        Returns a float between 0 and 1, or None on error.
        """
        end = "today"
        start = f"{period_days}daysAgo"
        response = self._run_report(
            date_ranges=[{"start_date": start, "end_date": end}],
            dimensions=["newVsReturning"],
            metrics=["transactions"],
        )
        if response is None or not response.rows:
            return None
        try:
            total = 0
            returning = 0
            for row in response.rows:
                dim = self._row_value(row, 0)
                txns = int(self._metric_value(row, 0))
                total += txns
                if dim == "returning":
                    returning = txns
            if total == 0:
                return 0.0
            return round(returning / total, 4)
        except Exception as exc:
            log.warning("ga4.parse_returning_error", error=str(exc))
            return None

    def get_top_pages(self, period_days: int = 30, limit: int = 10) -> list[dict]:
        """
        Return top `limit` pages by sessions.

        Each entry: {page_path, page_title, sessions, pageviews, avg_session_duration}
        """
        end = "today"
        start = f"{period_days}daysAgo"
        if not self.available:
            return []
        try:
            from google.analytics.data_v1beta.types import (
                DateRange,
                Dimension,
                Metric,
                OrderBy,
                RunReportRequest,
            )

            request = RunReportRequest(
                property=self._property,
                date_ranges=[DateRange(start_date=start, end_date=end)],
                dimensions=[Dimension(name="pagePath"), Dimension(name="pageTitle")],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="screenPageViews"),
                    Metric(name="averageSessionDuration"),
                ],
                order_bys=[
                    OrderBy(
                        metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                        desc=True,
                    )
                ],
                limit=limit,
            )
            response = self._client.run_report(request)
        except Exception as exc:
            log.warning("ga4.top_pages_error", error=str(exc))
            return []

        results = []
        for row in (response.rows or []):
            results.append({
                "page_path": self._row_value(row, 0),
                "page_title": self._row_value(row, 1),
                "sessions": int(self._metric_value(row, 0)),
                "pageviews": int(self._metric_value(row, 1)),
                "avg_session_duration": float(self._metric_value(row, 2)),
            })
        return results

    def get_traffic_sources(self, period_days: int = 30) -> list[dict]:
        """
        Return sessions grouped by sessionDefaultChannelGroup.

        Each entry: {channel, sessions, new_users}
        """
        response = self._run_report(
            date_ranges=[{"start_date": f"{period_days}daysAgo", "end_date": "today"}],
            dimensions=["sessionDefaultChannelGroup"],
            metrics=["sessions", "newUsers"],
        )
        if response is None:
            return []
        results = []
        for row in (response.rows or []):
            results.append({
                "channel": self._row_value(row, 0),
                "sessions": int(self._metric_value(row, 0)),
                "new_users": int(self._metric_value(row, 1)),
            })
        # Sort descending by sessions
        results.sort(key=lambda x: x["sessions"], reverse=True)
        return results

    def get_article_performance(
        self, page_path: str, period_days: int = 90
    ) -> Optional[dict]:
        """
        Return performance metrics for a specific page path.

        Returns:
            {sessions, pageviews, avg_session_duration, engagement_rate}
        or None on error / page not found.
        """
        end = "today"
        start = f"{period_days}daysAgo"
        if not self.available:
            return None
        try:
            from google.analytics.data_v1beta.types import (
                DateRange,
                Dimension,
                DimensionFilter,
                Filter,
                FilterExpression,
                Metric,
                RunReportRequest,
            )

            request = RunReportRequest(
                property=self._property,
                date_ranges=[DateRange(start_date=start, end_date=end)],
                dimensions=[Dimension(name="pagePath")],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="screenPageViews"),
                    Metric(name="averageSessionDuration"),
                    Metric(name="engagementRate"),
                ],
                dimension_filter=FilterExpression(
                    filter=Filter(
                        field_name="pagePath",
                        string_filter=Filter.StringFilter(
                            value=page_path,
                            match_type=Filter.StringFilter.MatchType.EXACT,
                        ),
                    )
                ),
            )
            response = self._client.run_report(request)
        except Exception as exc:
            log.warning("ga4.article_performance_error", error=str(exc))
            return None

        if not response.rows:
            return None
        try:
            row = response.rows[0]
            mv = lambda i: self._metric_value(row, i)
            return {
                "sessions": int(mv(0)),
                "pageviews": int(mv(1)),
                "avg_session_duration": float(mv(2)),
                "engagement_rate": float(mv(3)),
            }
        except Exception as exc:
            log.warning("ga4.parse_article_perf_error", error=str(exc))
            return None
