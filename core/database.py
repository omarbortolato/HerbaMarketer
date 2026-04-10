"""
core/database.py

SQLAlchemy 2.0 ORM models for HerbaMarketer.
Supports both SQLite (development) and PostgreSQL (production).

Run migrations with Alembic:
    alembic init alembic
    alembic revision --autogenerate -m "initial schema"
    alembic upgrade head
"""

import os
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

# ---------------------------------------------------------------------------
# Engine setup
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./herbamarketer.db")

# SQLite requires check_same_thread=False for use with web frameworks
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,  # set to True for SQL debug output
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Site(Base):
    """
    Sites configured in sites.yaml.
    One row per site managed by HerbaMarketer.
    """

    __tablename__ = "sites"

    id: int = Column(Integer, primary_key=True, index=True)
    slug: str = Column(String, unique=True, nullable=False)       # e.g. "herbago_it"
    url: str = Column(String, nullable=False)
    language: str = Column(String, nullable=False)                # e.g. "it"
    locale: str = Column(String, nullable=False)                  # e.g. "it-IT"
    mautic_campaign_id: Optional[int] = Column(Integer, nullable=True)
    email_prefix: Optional[str] = Column(String, nullable=True)   # e.g. "ITA"
    platform: str = Column(String, default="mautic")              # "mautic" | "brevo"
    active: bool = Column(Boolean, default=True)
    created_at: datetime = Column(DateTime, default=func.now())

    # Relationships
    email_pairs = relationship("EmailPair", back_populates="site")
    articles = relationship("Article", back_populates="site")
    publish_logs = relationship("PublishLog", back_populates="site")
    keyword_snapshots = relationship("KeywordSnapshot", back_populates="site")
    analytics_snapshots = relationship("AnalyticsSnapshot", back_populates="site")
    ads_snapshots = relationship("AdsSnapshot", back_populates="site")
    ads_campaign_snapshots = relationship("AdsCampaignSnapshot", back_populates="site")
    ads_daily_rows = relationship("AdsDailyRow", back_populates="site")
    gsc_daily_rows = relationship("GscDailyRow", back_populates="site")
    gsc_top_queries = relationship("GscTopQuery", back_populates="site")
    gsc_top_pages = relationship("GscTopPage", back_populates="site")
    ai_suggestions = relationship("AiSuggestion", back_populates="site")

    def __repr__(self) -> str:
        return f"<Site slug={self.slug!r} lang={self.language!r}>"


class ContentTopic(Base):
    """
    Content backlog — topics waiting to be processed.
    """

    __tablename__ = "content_topics"

    id: int = Column(Integer, primary_key=True, index=True)
    title: str = Column(Text, nullable=False)                     # topic description
    source: str = Column(String, nullable=False)                  # "seo_agent" | "email_input" | "manual" | "url_input"
    source_detail: Optional[str] = Column(Text, nullable=True)    # URL, email text, keyword query
    product_sku: Optional[str] = Column(String, nullable=True)    # associated product SKU
    product_url: Optional[str] = Column(String, nullable=True)    # IT product URL for CTA (optional override)
    status: str = Column(String, default="pending")               # "pending" | "approved" | "rejected" | "in_progress" | "done"
    priority: int = Column(Integer, default=5)
    created_at: datetime = Column(DateTime, default=func.now())

    # Relationships
    email_pairs = relationship("EmailPair", back_populates="topic")
    articles = relationship("Article", back_populates="topic")

    def __repr__(self) -> str:
        return f"<ContentTopic id={self.id} status={self.status!r} title={self.title[:40]!r}>"


class EmailPair(Base):
    """
    Generated email pair (problem email + product email) for a site/topic.
    """

    __tablename__ = "email_pairs"

    id: int = Column(Integer, primary_key=True, index=True)
    topic_id: Optional[int] = Column(Integer, ForeignKey("content_topics.id"), nullable=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    language: str = Column(String, nullable=False)
    email_1_subject: Optional[str] = Column(String, nullable=True)   # "problem" email
    email_1_body: Optional[str] = Column(Text, nullable=True)
    email_2_subject: Optional[str] = Column(String, nullable=True)   # "product" email
    email_2_body: Optional[str] = Column(Text, nullable=True)
    mautic_email_1_id: Optional[int] = Column(Integer, nullable=True)  # Mautic ID after publish
    mautic_email_2_id: Optional[int] = Column(Integer, nullable=True)
    status: str = Column(String, default="draft")                     # "draft" | "published" | "failed"
    published_at: Optional[datetime] = Column(DateTime, nullable=True)
    created_at: datetime = Column(DateTime, default=func.now())

    # Relationships
    topic = relationship("ContentTopic", back_populates="email_pairs")
    site = relationship("Site", back_populates="email_pairs")

    def __repr__(self) -> str:
        return f"<EmailPair id={self.id} site_id={self.site_id} status={self.status!r}>"


class Article(Base):
    """
    Generated or imported blog article for a site/topic.
    """

    __tablename__ = "articles"

    id: int = Column(Integer, primary_key=True, index=True)
    topic_id: Optional[int] = Column(Integer, ForeignKey("content_topics.id"), nullable=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    language: str = Column(String, nullable=False)
    title: Optional[str] = Column(String, nullable=True)
    slug: Optional[str] = Column(String, nullable=True)
    content: Optional[str] = Column(Text, nullable=True)
    excerpt: Optional[str] = Column(Text, nullable=True)              # WP excerpt / short description
    meta_title: Optional[str] = Column(String, nullable=True)
    meta_description: Optional[str] = Column(String, nullable=True)
    image_prompt: Optional[str] = Column(Text, nullable=True)
    image_url: Optional[str] = Column(String, nullable=True)
    wp_post_id: Optional[int] = Column(Integer, nullable=True)        # WordPress post ID
    wp_url: Optional[str] = Column(String, nullable=True)             # Direct WP post URL
    wp_published_at: Optional[datetime] = Column(DateTime, nullable=True)  # WP publication date
    word_count: Optional[int] = Column(Integer, nullable=True)        # Computed from content
    source: str = Column(String, default="generated")                 # "generated" | "wordpress_import"
    status: str = Column(String, default="draft")                     # "draft" | "pending_approval" | "published" | "failed" | "imported"
    published_at: Optional[datetime] = Column(DateTime, nullable=True)
    created_at: datetime = Column(DateTime, default=func.now())

    # Relationships
    topic = relationship("ContentTopic", back_populates="articles")
    site = relationship("Site", back_populates="articles")

    def __repr__(self) -> str:
        return f"<Article id={self.id} site_id={self.site_id} status={self.status!r}>"


class PublishLog(Base):
    """
    Audit log for every publish action (success or failure).
    """

    __tablename__ = "publish_log"

    id: int = Column(Integer, primary_key=True, index=True)
    entity_type: str = Column(String, nullable=False)    # "email_pair" | "article"
    entity_id: int = Column(Integer, nullable=False)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    action: str = Column(String, nullable=False)         # "published" | "failed" | "rejected"
    detail: Optional[str] = Column(Text, nullable=True)  # error message or detail
    created_at: datetime = Column(DateTime, default=func.now())

    # Relationships
    site = relationship("Site", back_populates="publish_logs")

    def __repr__(self) -> str:
        return f"<PublishLog id={self.id} entity={self.entity_type}/{self.entity_id} action={self.action!r}>"


class SiteStatusAck(Base):
    """
    Tracks when a site's errors were last acknowledged by the user.
    Failures before acked_at are ignored for the traffic-light status.
    """

    __tablename__ = "site_status_ack"

    site_id: int = Column(Integer, ForeignKey("sites.id"), primary_key=True)
    acked_at: datetime = Column(DateTime, nullable=False)

    def __repr__(self) -> str:
        return f"<SiteStatusAck site_id={self.site_id} acked_at={self.acked_at}>"


class KeywordSnapshot(Base):
    """
    Keyword research snapshot from DataForSEO.
    One row per keyword per site per snapshot date.
    """

    __tablename__ = "keyword_snapshots"

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    keyword: str = Column(String, nullable=False)
    search_volume: Optional[int] = Column(Integer, nullable=True)
    difficulty: Optional[int] = Column(Integer, nullable=True)
    trend_score: Optional[float] = Column(Float, nullable=True)
    snapshot_date: date = Column(Date, nullable=False)
    raw_data: Optional[dict] = Column(JSON, nullable=True)

    # Relationships
    site = relationship("Site", back_populates="keyword_snapshots")

    def __repr__(self) -> str:
        return f"<KeywordSnapshot keyword={self.keyword!r} date={self.snapshot_date}>"


class AnalyticsSnapshot(Base):
    """
    Daily GA4 analytics snapshot per site.
    Upserted on (site_id, snapshot_date, period_days) — one row per period per day.
    """

    __tablename__ = "analytics_snapshots"
    __table_args__ = (
        UniqueConstraint("site_id", "snapshot_date", "period_days", name="uq_analytics_snapshot"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    snapshot_date: date = Column(Date, nullable=False)
    period_days: int = Column(Integer, nullable=False, default=30)

    # Traffic
    sessions: Optional[int] = Column(Integer, nullable=True)
    total_users: Optional[int] = Column(Integer, nullable=True)
    new_users: Optional[int] = Column(Integer, nullable=True)
    engagement_rate: Optional[float] = Column(Float, nullable=True)
    avg_session_duration: Optional[float] = Column(Float, nullable=True)
    pageviews: Optional[int] = Column(Integer, nullable=True)

    # Ecommerce
    purchases: Optional[int] = Column(Integer, nullable=True)
    revenue: Optional[float] = Column(Float, nullable=True)
    avg_order_value: Optional[float] = Column(Float, nullable=True)
    add_to_carts: Optional[int] = Column(Integer, nullable=True)
    checkouts: Optional[int] = Column(Integer, nullable=True)
    cart_abandonment_rate: Optional[float] = Column(Float, nullable=True)
    returning_customer_rate: Optional[float] = Column(Float, nullable=True)

    # Aggregated breakdowns stored as JSON
    traffic_sources: Optional[dict] = Column(JSON, nullable=True)   # list of {channel, sessions, new_users}
    raw_overview: Optional[dict] = Column(JSON, nullable=True)
    raw_ecommerce: Optional[dict] = Column(JSON, nullable=True)

    created_at: datetime = Column(DateTime, default=func.now())

    # Relationships
    site = relationship("Site", back_populates="analytics_snapshots")

    def __repr__(self) -> str:
        return f"<AnalyticsSnapshot site_id={self.site_id} date={self.snapshot_date} period={self.period_days}d>"


class AdsSnapshot(Base):
    """
    Daily Google Ads account-level snapshot per site.
    Upserted on (site_id, snapshot_date, period_days).
    """

    __tablename__ = "ads_snapshots"
    __table_args__ = (
        UniqueConstraint("site_id", "snapshot_date", "period_days", name="uq_ads_snapshot"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    snapshot_date: date = Column(Date, nullable=False)
    period_days: int = Column(Integer, nullable=False, default=30)

    impressions: Optional[int] = Column(Integer, nullable=True)
    clicks: Optional[int] = Column(Integer, nullable=True)
    ctr: Optional[float] = Column(Float, nullable=True)
    cost: Optional[float] = Column(Float, nullable=True)
    conversions: Optional[float] = Column(Float, nullable=True)
    conversions_value: Optional[float] = Column(Float, nullable=True)
    roas: Optional[float] = Column(Float, nullable=True)

    created_at: datetime = Column(DateTime, default=func.now())

    site = relationship("Site", back_populates="ads_snapshots")

    def __repr__(self) -> str:
        return f"<AdsSnapshot site_id={self.site_id} date={self.snapshot_date} period={self.period_days}d>"


class AdsCampaignSnapshot(Base):
    """
    Daily Google Ads per-campaign snapshot per site.
    Upserted on (site_id, campaign_id, snapshot_date, period_days).
    """

    __tablename__ = "ads_campaign_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "site_id", "campaign_id", "snapshot_date", "period_days",
            name="uq_ads_campaign_snapshot",
        ),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    campaign_id: str = Column(String, nullable=False)
    campaign_name: Optional[str] = Column(String, nullable=True)
    status: Optional[str] = Column(String, nullable=True)          # ENABLED / PAUSED
    snapshot_date: date = Column(Date, nullable=False)
    period_days: int = Column(Integer, nullable=False, default=30)

    impressions: Optional[int] = Column(Integer, nullable=True)
    clicks: Optional[int] = Column(Integer, nullable=True)
    ctr: Optional[float] = Column(Float, nullable=True)
    cost: Optional[float] = Column(Float, nullable=True)
    conversions: Optional[float] = Column(Float, nullable=True)
    conversions_value: Optional[float] = Column(Float, nullable=True)
    roas: Optional[float] = Column(Float, nullable=True)

    created_at: datetime = Column(DateTime, default=func.now())

    site = relationship("Site", back_populates="ads_campaign_snapshots")

    def __repr__(self) -> str:
        return f"<AdsCampaignSnapshot site_id={self.site_id} campaign={self.campaign_name!r} date={self.snapshot_date}>"


class GscDailyRow(Base):
    """Day-granular Google Search Console data per site (account-level)."""

    __tablename__ = "gsc_daily_rows"
    __table_args__ = (UniqueConstraint("site_id", "row_date", name="uq_gsc_daily"),)

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    row_date: date = Column(Date, nullable=False, index=True)
    clicks: Optional[int] = Column(Integer, nullable=True)
    impressions: Optional[int] = Column(Integer, nullable=True)
    ctr: Optional[float] = Column(Float, nullable=True)      # 0.0–1.0
    position: Optional[float] = Column(Float, nullable=True)
    synced_at: datetime = Column(DateTime, default=func.now(), onupdate=func.now())

    site = relationship("Site", back_populates="gsc_daily_rows")


class GscTopQuery(Base):
    """Top search queries snapshot per site (upserted daily, period=30 days)."""

    __tablename__ = "gsc_top_queries"
    __table_args__ = (
        UniqueConstraint("site_id", "query", "snapshot_date", name="uq_gsc_query"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    snapshot_date: date = Column(Date, nullable=False)
    period_days: int = Column(Integer, default=30)
    query: str = Column(String, nullable=False)
    clicks: Optional[int] = Column(Integer, nullable=True)
    impressions: Optional[int] = Column(Integer, nullable=True)
    ctr: Optional[float] = Column(Float, nullable=True)      # as % (0–100)
    position: Optional[float] = Column(Float, nullable=True)

    site = relationship("Site", back_populates="gsc_top_queries")


class GscTopPage(Base):
    """Top pages snapshot per site (upserted daily, period=30 days)."""

    __tablename__ = "gsc_top_pages"
    __table_args__ = (
        UniqueConstraint("site_id", "page", "snapshot_date", name="uq_gsc_page"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    snapshot_date: date = Column(Date, nullable=False)
    period_days: int = Column(Integer, default=30)
    page: str = Column(String, nullable=False)
    clicks: Optional[int] = Column(Integer, nullable=True)
    impressions: Optional[int] = Column(Integer, nullable=True)
    ctr: Optional[float] = Column(Float, nullable=True)
    position: Optional[float] = Column(Float, nullable=True)

    site = relationship("Site", back_populates="gsc_top_pages")


class AiSuggestion(Base):
    """AI-generated daily suggestions for a site (analytics or ads)."""

    __tablename__ = "ai_suggestions"
    __table_args__ = (
        UniqueConstraint("site_id", "type", "suggestion_date", name="uq_ai_suggestion"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    type: str = Column(String, nullable=False)      # "analytics" | "ads"
    suggestion_date: date = Column(Date, nullable=False)
    bullets: Optional[list] = Column(JSON, nullable=True)   # list[str]
    generated_at: datetime = Column(DateTime, default=func.now())

    site = relationship("Site", back_populates="ai_suggestions")


class AdsDailyRow(Base):
    """
    Day-granular Google Ads data per site + campaign.
    One row per (site, campaign, date). campaign_id=None means account-level total.
    Upserted daily by the 07:00 job (yesterday's data) or on-demand backfill.
    """

    __tablename__ = "ads_daily_rows"
    __table_args__ = (
        UniqueConstraint("site_id", "campaign_id", "row_date", name="uq_ads_daily_row"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    campaign_id: Optional[str] = Column(String, nullable=True)   # None = account-level
    campaign_name: Optional[str] = Column(String, nullable=True)
    row_date: date = Column(Date, nullable=False, index=True)

    impressions: Optional[int] = Column(Integer, nullable=True)
    clicks: Optional[int] = Column(Integer, nullable=True)
    cost: Optional[float] = Column(Float, nullable=True)
    conversions: Optional[float] = Column(Float, nullable=True)
    conversions_value: Optional[float] = Column(Float, nullable=True)

    synced_at: datetime = Column(DateTime, default=func.now(), onupdate=func.now())

    site = relationship("Site", back_populates="ads_daily_rows")

    def __repr__(self) -> str:
        return f"<AdsDailyRow site_id={self.site_id} date={self.row_date} campaign={self.campaign_id!r}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_db() -> Session:
    """
    FastAPI/dependency-injection compatible session factory.

    Usage:
        from core.database import get_db
        db = next(get_db())

    Or as a FastAPI dependency:
        def route(db: Session = Depends(get_db)): ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    """
    Create all tables directly (development / initial setup).
    In production use Alembic: `alembic upgrade head`.
    """
    Base.metadata.create_all(bind=engine)
