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
    Generated blog article for a site/topic.
    """

    __tablename__ = "articles"

    id: int = Column(Integer, primary_key=True, index=True)
    topic_id: Optional[int] = Column(Integer, ForeignKey("content_topics.id"), nullable=True)
    site_id: Optional[int] = Column(Integer, ForeignKey("sites.id"), nullable=True)
    language: str = Column(String, nullable=False)
    title: Optional[str] = Column(String, nullable=True)
    slug: Optional[str] = Column(String, nullable=True)
    content: Optional[str] = Column(Text, nullable=True)
    meta_title: Optional[str] = Column(String, nullable=True)
    meta_description: Optional[str] = Column(String, nullable=True)
    image_prompt: Optional[str] = Column(Text, nullable=True)
    image_url: Optional[str] = Column(String, nullable=True)
    wp_post_id: Optional[int] = Column(Integer, nullable=True)        # WordPress post ID after publish
    status: str = Column(String, default="draft")                     # "draft" | "pending_approval" | "published" | "failed"
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
