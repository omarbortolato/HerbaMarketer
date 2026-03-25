"""
dashboard/app.py

FastAPI web dashboard for HerbaMarketer.

Pages:
  GET /              — Overview: all sites, traffic-light status, counters
  GET /sites/{slug}  — Site detail: email pairs, articles, next run
  GET /topics        — Topic backlog management (filterable)
  POST /topics/{id}/approve  — Approve a topic
  POST /topics/{id}/reject   — Reject a topic
  POST /topics/add           — Add a new manual topic
  GET /content/{type}/{id}   — View email pair or article
  GET /logs          — Publish log with filters
  GET /config        — Read-only config viewer

Run:
    uvicorn dashboard.app:app --reload --port 8000
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from config import get_all_active_sites, get_site_config
from core.database import (
    Article,
    ContentTopic,
    EmailPair,
    PublishLog,
    SessionLocal,
    Site,
    get_db,
)

app = FastAPI(title="HerbaMarketer Dashboard", version="1.0.0")

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

_STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _site_status(site_slug: str, db: Session) -> str:
    """
    Compute traffic-light status for a site.
    green  = published content in last 30 days
    yellow = last content 30-60 days ago
    red    = no content in 60+ days OR recent failures
    """
    site_db = db.query(Site).filter(Site.slug == site_slug).first()
    if not site_db:
        return "red"

    cutoff_30 = datetime.utcnow() - timedelta(days=30)
    cutoff_60 = datetime.utcnow() - timedelta(days=60)
    cutoff_7 = datetime.utcnow() - timedelta(days=7)

    recent_failures = (
        db.query(PublishLog)
        .filter(
            PublishLog.site_id == site_db.id,
            PublishLog.action == "failed",
            PublishLog.created_at >= cutoff_7,
        )
        .count()
    )
    if recent_failures > 0:
        return "red"

    recent_email = (
        db.query(EmailPair)
        .filter(
            EmailPair.site_id == site_db.id,
            EmailPair.status == "published",
            EmailPair.published_at >= cutoff_30,
        )
        .count()
    )
    recent_article = (
        db.query(Article)
        .filter(
            Article.site_id == site_db.id,
            Article.status == "published",
            Article.published_at >= cutoff_30,
        )
        .count()
    )
    if recent_email > 0 or recent_article > 0:
        return "green"

    # Check last 60 days
    semi_recent = (
        db.query(EmailPair)
        .filter(
            EmailPair.site_id == site_db.id,
            EmailPair.status == "published",
            EmailPair.published_at >= cutoff_60,
        )
        .count()
    )
    if semi_recent > 0:
        return "yellow"

    return "red"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request, db: Session = Depends(get_db)):
    """Overview: all sites with traffic-light status and global counters."""
    active_sites = get_all_active_sites()

    sites_data = []
    for site_cfg in active_sites:
        site_db = db.query(Site).filter(Site.slug == site_cfg.slug).first()
        email_count = (
            db.query(EmailPair)
            .filter(EmailPair.site_id == site_db.id, EmailPair.status == "published")
            .count()
            if site_db else 0
        )
        article_count = (
            db.query(Article)
            .filter(Article.site_id == site_db.id, Article.status == "published")
            .count()
            if site_db else 0
        )
        sites_data.append({
            "cfg": site_cfg,
            "status": _site_status(site_cfg.slug, db),
            "email_count": email_count,
            "article_count": article_count,
        })

    # Global counters
    total_pending = (
        db.query(ContentTopic).filter(ContentTopic.status == "pending").count()
    )
    total_approved = (
        db.query(ContentTopic).filter(ContentTopic.status == "approved").count()
    )
    total_emails = (
        db.query(EmailPair).filter(EmailPair.status == "published").count()
    )
    total_articles = (
        db.query(Article).filter(Article.status == "published").count()
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "sites": sites_data,
            "total_pending": total_pending,
            "total_approved": total_approved,
            "total_emails": total_emails,
            "total_articles": total_articles,
            "now": datetime.utcnow(),
        },
    )


@app.get("/sites/{slug}", response_class=HTMLResponse)
async def site_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    """Site detail: email pairs, articles, recent logs."""
    try:
        site_cfg = get_site_config(slug)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Site '{slug}' not found")

    site_db = db.query(Site).filter(Site.slug == slug).first()

    email_pairs = []
    articles = []
    recent_logs = []

    if site_db:
        email_pairs = (
            db.query(EmailPair)
            .filter(EmailPair.site_id == site_db.id)
            .order_by(EmailPair.created_at.desc())
            .limit(20)
            .all()
        )
        articles = (
            db.query(Article)
            .filter(Article.site_id == site_db.id)
            .order_by(Article.created_at.desc())
            .limit(20)
            .all()
        )
        recent_logs = (
            db.query(PublishLog)
            .filter(PublishLog.site_id == site_db.id)
            .order_by(PublishLog.created_at.desc())
            .limit(10)
            .all()
        )

    return templates.TemplateResponse(
        request=request,
        name="site_detail.html",
        context={
            "site": site_cfg,
            "site_db": site_db,
            "status": _site_status(slug, db),
            "email_pairs": email_pairs,
            "articles": articles,
            "recent_logs": recent_logs,
        },
    )


@app.get("/topics", response_class=HTMLResponse)
async def topics(
    request: Request,
    status: Optional[str] = None,
    source: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Topic backlog with optional filters."""
    query = db.query(ContentTopic)
    if status:
        query = query.filter(ContentTopic.status == status)
    if source:
        query = query.filter(ContentTopic.source == source)

    topic_list = query.order_by(
        ContentTopic.priority.desc(), ContentTopic.created_at.desc()
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="topics.html",
        context={
            "topics": topic_list,
            "filter_status": status or "",
            "filter_source": source or "",
            "statuses": ["pending", "approved", "rejected", "in_progress", "done"],
            "sources": ["manual", "seo_agent", "email_input", "url_input"],
        },
    )


@app.post("/topics/{topic_id}/approve")
async def approve_topic(topic_id: int, db: Session = Depends(get_db)):
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    topic.status = "approved"
    db.commit()
    return RedirectResponse(url="/topics", status_code=303)


@app.post("/topics/{topic_id}/reject")
async def reject_topic(topic_id: int, db: Session = Depends(get_db)):
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    topic.status = "rejected"
    db.commit()
    return RedirectResponse(url="/topics", status_code=303)


@app.post("/topics/add")
async def add_topic(
    title: str = Form(...),
    priority: int = Form(5),
    db: Session = Depends(get_db),
):
    topic = ContentTopic(
        title=title,
        source="manual",
        status="pending",
        priority=priority,
    )
    db.add(topic)
    db.commit()
    return RedirectResponse(url="/topics", status_code=303)


@app.get("/content/email/{pair_id}", response_class=HTMLResponse)
async def view_email_pair(
    pair_id: int, request: Request, db: Session = Depends(get_db)
):
    """View a generated email pair."""
    pair = db.query(EmailPair).filter(EmailPair.id == pair_id).first()
    if not pair:
        raise HTTPException(status_code=404, detail="EmailPair not found")

    site_db = db.query(Site).filter(Site.id == pair.site_id).first()
    topic = db.query(ContentTopic).filter(ContentTopic.id == pair.topic_id).first()

    return templates.TemplateResponse(
        request=request,
        name="content_email.html",
        context={
            "pair": pair,
            "site": site_db,
            "topic": topic,
        },
    )


@app.get("/content/article/{article_id}", response_class=HTMLResponse)
async def view_article(
    article_id: int, request: Request, db: Session = Depends(get_db)
):
    """View a generated article."""
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    site_db = db.query(Site).filter(Site.id == article.site_id).first()
    topic = db.query(ContentTopic).filter(ContentTopic.id == article.topic_id).first()

    return templates.TemplateResponse(
        request=request,
        name="content_article.html",
        context={
            "article": article,
            "site": site_db,
            "topic": topic,
        },
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs(
    request: Request,
    site_slug: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Publish log with optional filters."""
    query = db.query(PublishLog)

    if site_slug:
        site_db = db.query(Site).filter(Site.slug == site_slug).first()
        if site_db:
            query = query.filter(PublishLog.site_id == site_db.id)

    if action:
        query = query.filter(PublishLog.action == action)
    if entity_type:
        query = query.filter(PublishLog.entity_type == entity_type)

    log_entries = (
        query.order_by(PublishLog.created_at.desc()).limit(100).all()
    )

    # Attach site slug to each log entry for display
    site_map = {s.id: s.slug for s in db.query(Site).all()}

    active_sites = get_all_active_sites()

    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "logs": log_entries,
            "site_map": site_map,
            "filter_site": site_slug or "",
            "filter_action": action or "",
            "filter_entity": entity_type or "",
            "active_sites": active_sites,
        },
    )


@app.get("/config", response_class=HTMLResponse)
async def config_view(request: Request):
    """Read-only view of active site configurations."""
    active_sites = get_all_active_sites()

    return templates.TemplateResponse(
        request=request,
        name="config.html",
        context={
            "sites": active_sites,
        },
    )
