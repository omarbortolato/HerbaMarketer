"""
run_worker.py

Production entry point for the background worker:
- APScheduler (email job, article job, keyword research job)
- Telegram bot (polling mode)

Both run in the same process: scheduler in background threads,
Telegram bot in the main thread (blocking).

Usage:
    python run_worker.py
"""
from dotenv import load_dotenv
load_dotenv()

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./herbamarketer.db")

import structlog
from core.scheduler import start_scheduler
from core.telegram_bot import build_application

log = structlog.get_logger(__name__)


def main():
    log.info("worker_starting")

    # Start scheduler in background threads
    scheduler = start_scheduler()
    log.info("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])

    # Start Telegram bot (blocks until Ctrl+C)
    log.info("telegram_bot_starting")
    app = build_application()
    app.run_polling()

    scheduler.shutdown()
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
