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
  GET /login         — Login page
  POST /login        — Authenticate
  GET /logout        — Logout

Run:
    uvicorn dashboard.app:app --reload --port 8000
"""

import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import get_all_active_sites, get_site_config
from core.database import (
    Article,
    ContentTopic,
    EmailPair,
    PublishLog,
    SessionLocal,
    Site,
    SiteStatusAck,
    engine,
    get_db,
)
from core.database import create_tables


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run DB migrations on startup: create missing tables and add new columns."""
    # Create tables that don't exist yet (safe to call repeatedly)
    create_tables()
    # Add new columns to existing tables if missing (PostgreSQL IF NOT EXISTS)
    migrations = [
        "ALTER TABLE content_topics ADD COLUMN IF NOT EXISTS product_url VARCHAR",
    ]
    try:
        with engine.connect() as conn:
            for sql in migrations:
                try:
                    conn.execute(text(sql))
                except Exception:
                    pass  # column already exists or SQLite (no IF NOT EXISTS)
            conn.commit()
    except Exception:
        pass
    yield


app = FastAPI(title="HerbaMarketer Dashboard", version="1.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_SESSION_SECRET = os.getenv("SESSION_SECRET_KEY", "herbamarketer-dashboard-secret-key-change-in-prod")

# SHA-256 hashed passwords (no plaintext stored)
_USERS: dict[str, str] = {
    "omar": hashlib.sha256("herbamarketerschei26!".encode()).hexdigest(),
    "emiliano": hashlib.sha256("herbamarketerschei26!".encode()).hexdigest(),
}

_PUBLIC_PATHS = {"/login"}
_PUBLIC_PREFIXES = ("/static",)


def _check_password(username: str, password: str) -> bool:
    stored = _USERS.get(username)
    if not stored:
        return False
    candidate = hashlib.sha256(password.encode()).hexdigest()
    return secrets.compare_digest(stored, candidate)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)
        if not request.session.get("user"):
            return RedirectResponse(url="/login", status_code=302)
        return await call_next(request)


# Middleware must be added before mounting static files
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET)

# ---------------------------------------------------------------------------
# Static files & templates
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

_STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _site_status(site_slug: str, db: Session) -> dict:
    """
    Compute traffic-light status for a site.
    Returns dict with 'status' (green/yellow/red) and 'detail' (reason string).

    green  = published content in last 30 days
    yellow = last content 30-60 days ago
    red    = no content in 60+ days OR recent un-acknowledged failures
    """
    site_db = db.query(Site).filter(Site.slug == site_slug).first()
    if not site_db:
        return {"status": "red", "detail": "Sito non ancora nel DB"}

    now = datetime.utcnow()
    cutoff_30 = now - timedelta(days=30)
    cutoff_60 = now - timedelta(days=60)
    cutoff_7 = now - timedelta(days=7)

    # Check if errors are acknowledged
    ack = db.query(SiteStatusAck).filter(SiteStatusAck.site_id == site_db.id).first()
    failure_cutoff = ack.acked_at if ack else cutoff_7  # if acked, only look after ack time

    last_failure = (
        db.query(PublishLog)
        .filter(
            PublishLog.site_id == site_db.id,
            PublishLog.action == "failed",
            PublishLog.created_at >= failure_cutoff,
            PublishLog.created_at >= cutoff_7,  # always at most 7 days back
        )
        .order_by(PublishLog.created_at.desc())
        .first()
    )
    if last_failure:
        detail = last_failure.detail or "Errore senza dettagli"
        short = detail[:120] + "..." if len(detail) > 120 else detail
        return {"status": "red", "detail": f"Errore recente: {short}"}

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
            Article.status.in_(["pending_approval", "published"]),
            Article.created_at >= cutoff_30,
        )
        .count()
    )
    if recent_email > 0 or recent_article > 0:
        return {"status": "green", "detail": "Contenuto pubblicato negli ultimi 30 giorni"}

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
        return {"status": "yellow", "detail": "Nessun contenuto negli ultimi 30 giorni"}

    return {"status": "red", "detail": "Nessun contenuto pubblicato negli ultimi 60 giorni"}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page."""
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None},
    )


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Authenticate and set session cookie."""
    if _check_password(username.strip().lower(), password):
        request.session["user"] = username.strip().lower()
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "Username o password non validi."},
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request, triggered: Optional[str] = None, db: Session = Depends(get_db)):
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
            .filter(
                Article.site_id == site_db.id,
                Article.status.in_(["pending_approval", "published"]),
            )
            .count()
            if site_db else 0
        )
        status_info = _site_status(site_cfg.slug, db)
        sites_data.append({
            "cfg": site_cfg,
            "status": status_info["status"],
            "detail": status_info["detail"],
            "email_count": email_count,
            "article_count": article_count,
            "site_db_id": site_db.id if site_db else None,
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
        db.query(Article)
        .filter(Article.status.in_(["pending_approval", "published"]))
        .count()
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
            "triggered": triggered,
            "current_user": request.session.get("user"),
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
            "status": _site_status(slug, db)["status"],
            "email_pairs": email_pairs,
            "articles": articles,
            "recent_logs": recent_logs,
            "current_user": request.session.get("user"),
        },
    )


@app.get("/topics", response_class=HTMLResponse)
async def topics(
    request: Request,
    status: Optional[str] = None,
    source: Optional[str] = None,
    sort: Optional[str] = "id",
    order: Optional[str] = "asc",
    db: Session = Depends(get_db),
):
    """Topic backlog with optional filters and column sorting."""
    query = db.query(ContentTopic)
    if status:
        query = query.filter(ContentTopic.status == status)
    if source:
        query = query.filter(ContentTopic.source == source)

    _sort_columns = {
        "id": ContentTopic.id,
        "priority": ContentTopic.priority,
        "status": ContentTopic.status,
        "source": ContentTopic.source,
        "created_at": ContentTopic.created_at,
    }
    sort_col = _sort_columns.get(sort, ContentTopic.id)
    if order == "desc":
        query = query.order_by(sort_col.desc())
    else:
        query = query.order_by(sort_col.asc())

    topic_list = query.all()

    return templates.TemplateResponse(
        request=request,
        name="topics.html",
        context={
            "topics": topic_list,
            "filter_status": status or "",
            "filter_source": source or "",
            "sort": sort,
            "order": order,
            "statuses": ["pending", "approved", "email_done", "article_done", "done", "rejected", "in_progress"],
            "sources": ["manual", "seo_agent", "email_input", "url_input"],
            "current_user": request.session.get("user"),
        },
    )


@app.post("/topics/{topic_id}/reactivate")
async def reactivate_topic(topic_id: int, db: Session = Depends(get_db)):
    """Reset a done/email_done/article_done topic back to approved."""
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    topic.status = "approved"
    db.commit()
    return RedirectResponse(url="/topics", status_code=303)


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
    product_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    topic = ContentTopic(
        title=title,
        source="manual",
        status="pending",
        priority=priority,
        product_url=product_url.strip() if product_url and product_url.strip() else None,
    )
    db.add(topic)
    db.commit()
    return RedirectResponse(url="/topics", status_code=303)


@app.post("/sites/{slug}/ack-errors")
async def ack_site_errors(slug: str, db: Session = Depends(get_db)):
    """Mark all current errors for a site as acknowledged."""
    site_db = db.query(Site).filter(Site.slug == slug).first()
    if not site_db:
        raise HTTPException(status_code=404, detail="Site not found")

    ack = db.query(SiteStatusAck).filter(SiteStatusAck.site_id == site_db.id).first()
    if ack:
        ack.acked_at = datetime.utcnow()
    else:
        db.add(SiteStatusAck(site_id=site_db.id, acked_at=datetime.utcnow()))
    db.commit()
    return RedirectResponse(url="/", status_code=303)


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
            "current_user": request.session.get("user"),
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
            "current_user": request.session.get("user"),
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
            "current_user": request.session.get("user"),
        },
    )


@app.post("/run/email-job")
async def run_email_job(background_tasks: BackgroundTasks):
    """Manually trigger email_job in background."""
    from core.scheduler import email_job
    background_tasks.add_task(email_job)
    return RedirectResponse(url="/?triggered=email", status_code=303)


@app.post("/run/article-job")
async def run_article_job(background_tasks: BackgroundTasks):
    """Manually trigger article_job in background."""
    from core.scheduler import article_job
    background_tasks.add_task(article_job)
    return RedirectResponse(url="/?triggered=article", status_code=303)


@app.get("/config", response_class=HTMLResponse)
async def config_view(request: Request, saved: Optional[str] = None):
    """Editable view of site configurations and global settings."""
    from config import get_settings
    active_sites = get_all_active_sites()
    s = get_settings()

    return templates.TemplateResponse(
        request=request,
        name="config.html",
        context={
            "sites": active_sites,
            "settings": s,
            "saved": saved,
            "current_user": request.session.get("user"),
        },
    )


@app.post("/config/save-site/{slug}")
async def config_save_site(
    slug: str,
    mautic_campaign_id: Optional[str] = Form(None),
    email_prefix: Optional[str] = Form(None),
    preferred_customer_url: Optional[str] = Form(None),
    distributor_url: Optional[str] = Form(None),
    wp_author_name: Optional[str] = Form(None),
):
    """Save editable fields for one site to sites.yaml."""
    from config import save_site_field
    fields = {
        "mautic_campaign_id": mautic_campaign_id,
        "email_prefix": email_prefix,
        "preferred_customer_url": preferred_customer_url,
        "distributor_url": distributor_url,
        "wp_author_name": wp_author_name or None,
    }
    for field, value in fields.items():
        if value is not None:
            save_site_field(slug, field, value or None)
    return RedirectResponse(url="/config?saved=site", status_code=303)


@app.post("/config/save-settings")
async def config_save_settings(
    email_interval: int = Form(...),
    article_interval: int = Form(...),
    keyword_interval: int = Form(...),
):
    """Save scheduler intervals to settings.yaml."""
    from config import save_scheduler_settings
    save_scheduler_settings(email_interval, article_interval, keyword_interval)
    return RedirectResponse(url="/config?saved=settings", status_code=303)


@app.post("/config/add-site")
async def config_add_site(
    slug: str = Form(...),
    url: str = Form(...),
    language: str = Form(...),
    locale: str = Form(...),
    platform: str = Form(...),
    wp_api_url: Optional[str] = Form(None),
    mautic_campaign_id: Optional[str] = Form(None),
    email_prefix: Optional[str] = Form(None),
    brevo_list_id: Optional[str] = Form(None),
    preferred_customer_url: Optional[str] = Form(None),
    distributor_url: Optional[str] = Form(None),
    wp_author_name: Optional[str] = Form(None),
):
    """Add a new site to sites.yaml."""
    from config import add_site
    try:
        add_site(
            slug=slug.strip().replace("-", "_"),
            url=url.strip().rstrip("/"),
            language=language.strip(),
            locale=locale.strip(),
            platform=platform.strip(),
            wp_api_url=wp_api_url.strip() if wp_api_url and wp_api_url.strip() else None,
            mautic_campaign_id=int(mautic_campaign_id) if mautic_campaign_id and mautic_campaign_id.strip() else None,
            email_prefix=email_prefix.strip() if email_prefix and email_prefix.strip() else None,
            brevo_list_id=int(brevo_list_id) if brevo_list_id and brevo_list_id.strip() else None,
            preferred_customer_url=preferred_customer_url.strip() if preferred_customer_url and preferred_customer_url.strip() else None,
            distributor_url=distributor_url.strip() if distributor_url and distributor_url.strip() else None,
            wp_author_name=wp_author_name.strip() if wp_author_name and wp_author_name.strip() else None,
        )
    except ValueError as exc:
        # Slug already exists — redirect with error param
        return RedirectResponse(url=f"/config?saved=error&msg={exc}", status_code=303)
    return RedirectResponse(url="/config?saved=site_added", status_code=303)
