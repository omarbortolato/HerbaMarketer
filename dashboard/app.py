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
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import nullsfirst, nullslast, text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import get_all_active_sites, get_site_config
from core.database import (
    AdsCampaignSnapshot,
    AdsSnapshot,
    AnalyticsSnapshot,
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

# In-memory cache for live GA4 calls: {cache_key: (timestamp, data)}
import time as _time
_analytics_cache: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run DB migrations on startup: create missing tables and add new columns."""
    # Create tables that don't exist yet (safe to call repeatedly)
    create_tables()
    # Add new columns to existing tables if missing (PostgreSQL IF NOT EXISTS)
    migrations = [
        "ALTER TABLE content_topics ADD COLUMN IF NOT EXISTS product_url VARCHAR",
        "ALTER TABLE articles ADD COLUMN IF NOT EXISTS excerpt TEXT",
        "ALTER TABLE articles ADD COLUMN IF NOT EXISTS wp_url VARCHAR",
        "ALTER TABLE articles ADD COLUMN IF NOT EXISTS wp_published_at TIMESTAMP",
        "ALTER TABLE articles ADD COLUMN IF NOT EXISTS word_count INTEGER",
        "ALTER TABLE articles ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'generated'",
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
            "active_sites": get_all_active_sites(),
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
async def site_detail(
    slug: str,
    request: Request,
    art_sort: Optional[str] = "wp_published_at",
    art_order: Optional[str] = "desc",
    art_filter: Optional[str] = None,
    synced: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Site detail: email pairs, generated articles, imported articles, recent logs."""
    try:
        site_cfg = get_site_config(slug)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Site '{slug}' not found")

    site_db = db.query(Site).filter(Site.slug == slug).first()

    email_pairs = []
    generated_articles = []
    imported_articles = []
    recent_logs = []

    _art_sort_cols = {
        "title": Article.title,
        "wp_published_at": Article.wp_published_at,
        "word_count": Article.word_count,
        "status": Article.status,
        "created_at": Article.created_at,
    }

    if site_db:
        email_pairs = (
            db.query(EmailPair)
            .filter(EmailPair.site_id == site_db.id)
            .order_by(EmailPair.created_at.desc())
            .limit(20)
            .all()
        )

        # Generated articles (created by HerbaMarketer)
        generated_articles = (
            db.query(Article)
            .filter(Article.site_id == site_db.id, Article.source == "generated")
            .order_by(Article.created_at.desc())
            .limit(20)
            .all()
        )

        # Imported articles from WP — sortable + filterable
        imp_query = db.query(Article).filter(
            Article.site_id == site_db.id,
            Article.source == "wordpress_import",
        )
        if art_filter:
            imp_query = imp_query.filter(Article.status == art_filter)
        sort_col = _art_sort_cols.get(art_sort, Article.wp_published_at)
        if art_order == "asc":
            imp_query = imp_query.order_by(sort_col.asc())
        else:
            imp_query = imp_query.order_by(sort_col.desc())
        imported_articles = imp_query.all()

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
            "articles": generated_articles,
            "imported_articles": imported_articles,
            "art_sort": art_sort,
            "art_order": art_order,
            "art_filter": art_filter or "",
            "synced": synced,
            "recent_logs": recent_logs,
            "current_user": request.session.get("user"),
        },
    )


@app.post("/sites/{slug}/sync-articles")
async def sync_articles(slug: str, background_tasks: BackgroundTasks):
    """Trigger WordPress article import for a site in background."""
    from publishers.wp_importer import import_existing_articles

    def _run():
        import structlog
        _log = structlog.get_logger(__name__)
        try:
            stats = import_existing_articles(slug)
            _log.info(
                "wp_import_done",
                site=slug,
                inserted=stats.inserted,
                updated=stats.updated,
                skipped=stats.skipped,
            )
        except Exception as exc:
            _log.error("wp_import_failed", site=slug, error=str(exc))

    background_tasks.add_task(_run)
    return RedirectResponse(
        url=f"/sites/{slug}?synced=1", status_code=303
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
            "active_sites": get_all_active_sites(),
            "current_user": request.session.get("user"),
        },
    )


@app.get("/articles", response_class=HTMLResponse)
async def articles_view(
    request: Request,
    site: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    sort: Optional[str] = "wp_published_at",
    order: Optional[str] = "desc",
    db: Session = Depends(get_db),
):
    """Content inventory: all articles across all sites, filterable and sortable."""
    query = db.query(Article, Site).join(Site, Article.site_id == Site.id, isouter=True)

    if site:
        query = query.filter(Site.slug == site)
    if status:
        query = query.filter(Article.status == status)
    if source:
        query = query.filter(Article.source == source)

    _sort_cols = {
        "title": Article.title,
        "wp_published_at": Article.wp_published_at,
        "word_count": Article.word_count,
        "status": Article.status,
        "site": Site.slug,
        "created_at": Article.created_at,
    }
    sort_col = _sort_cols.get(sort, Article.wp_published_at)
    if order == "desc":
        query = query.order_by(sort_col.desc().nullslast())
    else:
        query = query.order_by(sort_col.asc().nullsfirst())

    rows = query.all()
    # rows is list of (Article, Site) tuples
    article_list = [{"article": a, "site": s} for a, s in rows]

    active_sites = get_all_active_sites()

    return templates.TemplateResponse(
        request=request,
        name="articles.html",
        context={
            "article_list": article_list,
            "filter_site": site or "",
            "filter_status": status or "",
            "filter_source": source or "",
            "sort": sort,
            "order": order,
            "active_sites": active_sites,
            "total": len(article_list),
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


@app.get("/topics/{topic_id}/duplicates")
async def check_topic_duplicates(topic_id: int, db: Session = Depends(get_db)):
    """
    Return articles with titles similar to the given topic.
    Uses the first 4 significant words (>3 chars) of the topic title
    as LIKE conditions (all must match — AND logic).
    """
    from fastapi.responses import JSONResponse

    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        return JSONResponse({"duplicates": []})

    # Extract first 4 words longer than 3 chars (skip common short words)
    words = [w for w in topic.title.lower().split() if len(w) > 3][:4]
    if not words:
        return JSONResponse({"duplicates": []})

    query = db.query(Article, Site).join(Site, Article.site_id == Site.id, isouter=True)
    for word in words:
        query = query.filter(Article.title.ilike(f"%{word}%"))

    rows = query.limit(5).all()

    duplicates = []
    for art, site in rows:
        duplicates.append({
            "id": art.id,
            "title": art.title or "",
            "site_slug": site.slug if site else "",
            "wp_url": art.wp_url or "",
            "wp_published_at": art.wp_published_at.strftime("%d/%m/%Y") if art.wp_published_at else "",
            "status": art.status or "",
        })

    return JSONResponse({"duplicates": duplicates})


@app.post("/topics/{topic_id}/approve")
async def approve_topic(
    topic_id: int,
    product_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    topic.status = "approved"
    if product_url and product_url.strip():
        topic.product_url = product_url.strip()
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


@app.post("/topics/{topic_id}/delete")
async def delete_topic(topic_id: int, db: Session = Depends(get_db)):
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    db.delete(topic)
    db.commit()
    return RedirectResponse(url="/topics", status_code=303)


@app.post("/topics/{topic_id}/reset")
async def reset_topic(topic_id: int, db: Session = Depends(get_db)):
    """Reset a stuck in_progress topic back to approved so it can be retried."""
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    topic.status = "approved"
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
async def run_email_job(
    background_tasks: BackgroundTasks,
    request: Request,
    sites: List[str] = Form(default=[]),
):
    """Manually trigger email_job in background, optionally restricted to selected sites."""
    from core.scheduler import email_job
    site_slugs = sites if sites else None
    background_tasks.add_task(email_job, site_slugs)
    return RedirectResponse(url="/?triggered=email", status_code=303)


@app.post("/run/article-job")
async def run_article_job(
    background_tasks: BackgroundTasks,
    request: Request,
    sites: List[str] = Form(default=[]),
):
    """Manually trigger article_job in background, optionally restricted to selected sites."""
    from core.scheduler import article_job
    site_slugs = sites if sites else None
    background_tasks.add_task(article_job, site_slugs)
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


# ---------------------------------------------------------------------------
# Analytics routes
# ---------------------------------------------------------------------------

_ANALYTICS_CACHE_TTL = 3600  # 1 hour


def _cache_get(key: str):
    entry = _analytics_cache.get(key)
    if entry and (_time.time() - entry[0]) < _ANALYTICS_CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, data):
    _analytics_cache[key] = (_time.time(), data)


_VALID_PERIODS = (7, 30, 90)


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_overview(
    request: Request,
    period: int = Query(30),
    db: Session = Depends(get_db),
):
    """Overview analytics: latest snapshot per site for the selected period."""
    user = request.session.get("user")
    if period not in _VALID_PERIODS:
        period = 30
    active_sites = get_all_active_sites()

    rows = []
    for site_cfg in active_sites:
        site_db = db.query(Site).filter(Site.slug == site_cfg.slug).first()
        snap = None
        if site_db:
            snap = (
                db.query(AnalyticsSnapshot)
                .filter(
                    AnalyticsSnapshot.site_id == site_db.id,
                    AnalyticsSnapshot.period_days == period,
                )
                .order_by(AnalyticsSnapshot.snapshot_date.desc())
                .first()
            )
        rows.append({"site": site_cfg, "snap": snap})

    return templates.TemplateResponse(
        "analytics/index.html",
        {
            "request": request,
            "current_user": user,
            "rows": rows,
            "period": period,
            "valid_periods": _VALID_PERIODS,
        },
    )


@app.get("/analytics/{site_slug}", response_class=HTMLResponse)
async def analytics_site_detail(
    site_slug: str,
    request: Request,
    period: int = Query(30),
    db: Session = Depends(get_db),
):
    """Per-site analytics detail with live top pages and traffic sources (cached 1h)."""
    user = request.session.get("user")
    if period not in _VALID_PERIODS:
        period = 30

    try:
        site_cfg = get_site_config(site_slug)
    except KeyError:
        raise HTTPException(status_code=404, detail="Site not found")

    site_db = db.query(Site).filter(Site.slug == site_slug).first()
    snap = None
    if site_db:
        snap = (
            db.query(AnalyticsSnapshot)
            .filter(
                AnalyticsSnapshot.site_id == site_db.id,
                AnalyticsSnapshot.period_days == period,
            )
            .order_by(AnalyticsSnapshot.snapshot_date.desc())
            .first()
        )

    # Live data (cached 1h per period)
    top_pages = []
    traffic_sources = []
    ga4_available = bool(
        site_cfg.ga4_property_id and site_cfg.ga4_property_id != "DA_AGGIUNGERE"
    )
    if ga4_available:
        cache_key_pages = f"top_pages:{site_slug}:{period}"
        cache_key_sources = f"traffic_sources:{site_slug}:{period}"
        top_pages = _cache_get(cache_key_pages)
        traffic_sources = _cache_get(cache_key_sources)
        if top_pages is None or traffic_sources is None:
            from core.ga4_client import GA4Client
            client = GA4Client(site_cfg.ga4_property_id)
            if client.available:
                top_pages = client.get_top_pages(period_days=period)
                traffic_sources = client.get_traffic_sources(period_days=period)
                _cache_set(cache_key_pages, top_pages)
                _cache_set(cache_key_sources, traffic_sources)
            else:
                top_pages = top_pages or []
                traffic_sources = traffic_sources or []

    return templates.TemplateResponse(
        "analytics/site_detail.html",
        {
            "request": request,
            "current_user": user,
            "site": site_cfg,
            "snap": snap,
            "top_pages": top_pages,
            "traffic_sources": traffic_sources,
            "ga4_available": ga4_available,
            "period": period,
            "valid_periods": _VALID_PERIODS,
        },
    )


@app.post("/analytics/{site_slug}/sync")
async def analytics_sync_site(
    site_slug: str,
    background_tasks: BackgroundTasks,
    request: Request,
    period: int = Query(30),
):
    """Force a GA4 sync for one site for all periods (runs in background)."""
    from core.analytics_sync import sync_site_analytics

    def _do_sync():
        # Sync all periods so any tab has fresh data
        for p in _VALID_PERIODS:
            sync_site_analytics(site_slug, period_days=p)
        # Invalidate all cached entries for this site
        for key in list(_analytics_cache.keys()):
            if f":{site_slug}" in key:
                _analytics_cache.pop(key, None)

    background_tasks.add_task(_do_sync)
    return RedirectResponse(url=f"/analytics/{site_slug}?period={period}&syncing=1", status_code=303)


@app.get("/api/article/{article_id}/analytics")
async def api_article_analytics(article_id: int, db: Session = Depends(get_db)):
    """Return GA4 performance for a specific article (lazy-loaded by the UI)."""
    from fastapi.responses import JSONResponse

    article = db.query(Article).filter(Article.id == article_id).first()
    if not article or not article.wp_url:
        return JSONResponse({"error": "article not found or no WP url"}, status_code=404)

    site_db = db.query(Site).filter(Site.id == article.site_id).first()
    if not site_db:
        return JSONResponse({"error": "site not found"}, status_code=404)

    try:
        site_cfg = get_site_config(site_db.slug)
    except KeyError:
        return JSONResponse({"error": "site config not found"}, status_code=404)

    if not site_cfg.ga4_property_id or site_cfg.ga4_property_id == "DA_AGGIUNGERE":
        return JSONResponse({"error": "no GA4 property configured"}, status_code=404)

    cache_key = f"article_perf:{article_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    from urllib.parse import urlparse
    parsed = urlparse(article.wp_url)
    page_path = parsed.path or "/"

    from core.ga4_client import GA4Client
    client = GA4Client(site_cfg.ga4_property_id)
    if not client.available:
        return JSONResponse({"error": "GA4 client not available"}, status_code=503)

    perf = client.get_article_performance(page_path, period_days=90)
    if perf is None:
        return JSONResponse({"sessions": 0, "pageviews": 0, "avg_session_duration": 0, "engagement_rate": 0})

    _cache_set(cache_key, perf)
    return JSONResponse(perf)


# ---------------------------------------------------------------------------
# Google Ads routes
# ---------------------------------------------------------------------------


@app.get("/ads", response_class=HTMLResponse)
async def ads_overview(
    request: Request,
    period: int = Query(30),
    db: Session = Depends(get_db),
):
    """Overview Google Ads: account-level snapshot per site."""
    user = request.session.get("user")
    if period not in _VALID_PERIODS:
        period = 30
    active_sites = get_all_active_sites()

    rows = []
    for site_cfg in active_sites:
        if not site_cfg.google_ads_customer_id:
            continue
        site_db = db.query(Site).filter(Site.slug == site_cfg.slug).first()
        snap = None
        if site_db:
            snap = (
                db.query(AdsSnapshot)
                .filter(
                    AdsSnapshot.site_id == site_db.id,
                    AdsSnapshot.period_days == period,
                )
                .order_by(AdsSnapshot.snapshot_date.desc())
                .first()
            )
        rows.append({"site": site_cfg, "snap": snap})

    return templates.TemplateResponse(
        "ads/index.html",
        {
            "request": request,
            "current_user": user,
            "rows": rows,
            "period": period,
            "valid_periods": _VALID_PERIODS,
        },
    )


@app.get("/ads/{site_slug}", response_class=HTMLResponse)
async def ads_site_detail(
    site_slug: str,
    request: Request,
    period: int = Query(30),
    db: Session = Depends(get_db),
):
    """Per-site Google Ads detail: account overview + campaign breakdown."""
    user = request.session.get("user")
    if period not in _VALID_PERIODS:
        period = 30

    try:
        site_cfg = get_site_config(site_slug)
    except KeyError:
        raise HTTPException(status_code=404, detail="Site not found")

    ads_available = bool(site_cfg.google_ads_customer_id)

    site_db = db.query(Site).filter(Site.slug == site_slug).first()
    snap = None
    campaigns = []
    if site_db:
        snap = (
            db.query(AdsSnapshot)
            .filter(
                AdsSnapshot.site_id == site_db.id,
                AdsSnapshot.period_days == period,
            )
            .order_by(AdsSnapshot.snapshot_date.desc())
            .first()
        )
        campaigns = (
            db.query(AdsCampaignSnapshot)
            .filter(
                AdsCampaignSnapshot.site_id == site_db.id,
                AdsCampaignSnapshot.period_days == period,
                AdsCampaignSnapshot.snapshot_date == (snap.snapshot_date if snap else None),
            )
            .order_by(AdsCampaignSnapshot.cost.desc())
            .all()
            if snap else []
        )

    return templates.TemplateResponse(
        "ads/site_detail.html",
        {
            "request": request,
            "current_user": user,
            "site": site_cfg,
            "snap": snap,
            "campaigns": campaigns,
            "ads_available": ads_available,
            "period": period,
            "valid_periods": _VALID_PERIODS,
        },
    )


@app.post("/ads/{site_slug}/sync")
async def ads_sync_site(
    site_slug: str,
    background_tasks: BackgroundTasks,
    request: Request,
    period: int = Query(30),
):
    """Force a Google Ads sync for one site (all periods, runs in background)."""
    from core.ads_sync import sync_site_ads

    def _do_sync():
        for p in _VALID_PERIODS:
            sync_site_ads(site_slug, period_days=p)

    background_tasks.add_task(_do_sync)
    return RedirectResponse(url=f"/ads/{site_slug}?period={period}&syncing=1", status_code=303)


# ---------------------------------------------------------------------------
# Temporary debug endpoint — remove after diagnosis
# ---------------------------------------------------------------------------

@app.get("/api/debug/db")
async def debug_db(db: Session = Depends(get_db)):
    """Show DB counts and latest analytics snapshots for diagnosis."""
    from fastapi.responses import JSONResponse
    from sqlalchemy import inspect, text

    # Check which tables exist
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    # Count rows per table
    counts = {}
    for t in ["sites", "analytics_snapshots", "ads_snapshots", "ads_campaign_snapshots"]:
        if t in tables:
            try:
                counts[t] = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            except Exception as e:
                counts[t] = f"error: {e}"
        else:
            counts[t] = "TABLE MISSING"

    # Latest analytics snapshots
    snaps = db.query(AnalyticsSnapshot).order_by(AnalyticsSnapshot.id.desc()).limit(10).all()
    snap_data = [
        {
            "id": s.id,
            "site_id": s.site_id,
            "snapshot_date": str(s.snapshot_date),
            "period_days": s.period_days,
            "sessions": s.sessions,
            "pageviews": s.pageviews,
        }
        for s in snaps
    ]

    # Sites in DB
    sites = db.query(Site).all()
    site_data = [{"id": s.id, "slug": s.slug} for s in sites]

    return JSONResponse({
        "tables": tables,
        "counts": counts,
        "sites_in_db": site_data,
        "latest_analytics_snapshots": snap_data,
    })
