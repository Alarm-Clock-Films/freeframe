"""Scheduled housekeeping triggered over HTTP, for hosts without Celery beat.

On Vercel there is no long-lived `celery beat` process. Vercel Cron calls
these endpoints instead (the schedules are in vercel.json and mirror
celery_app.beat_schedule) with `Authorization: Bearer <CRON_SECRET>`. Each call
only enqueues the task; a queue worker runs it, exactly as when beat publishes
it.

With CRON_SECRET unset every endpoint here answers 404, so a deployment that
still runs beat exposes nothing new.
"""
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, status

from ..config import settings
from ..tasks.celery_app import send_task_safe

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/cron", tags=["internal"], include_in_schema=False)


def _cron_tasks() -> dict:
    """Job name in the URL -> Celery task. Imported lazily: the task modules
    pull in the transcoder and S3 clients, which most requests never need."""
    from ..tasks.cleanup_tasks import (
        cleanup_soft_deleted,
        reap_stale_uploads,
        requeue_stuck_processing,
        sweep_orphan_s3,
    )
    from ..tasks.reminder_tasks import send_due_date_reminders

    return {
        "due-date-reminders": send_due_date_reminders,
        "reap-stale-uploads": reap_stale_uploads,
        "requeue-stuck-processing": requeue_stuck_processing,
        "cleanup-soft-deleted": cleanup_soft_deleted,
        "sweep-orphan-s3": sweep_orphan_s3,
    }


# The job names, which are also the keys of celery_app.beat_schedule.
CRON_JOBS = (
    "due-date-reminders",
    "reap-stale-uploads",
    "requeue-stuck-processing",
    "cleanup-soft-deleted",
    "sweep-orphan-s3",
)


def _authorized(authorization: str | None) -> bool:
    secret = settings.cron_secret
    if not secret or not authorization:
        return False
    return hmac.compare_digest(authorization.encode(), f"Bearer {secret}".encode())


@router.get("/{job}")
def run_cron_job(job: str, authorization: str | None = Header(None)):
    # 404 rather than 401 while disabled, so the endpoints are not advertised.
    if not settings.cron_secret or job not in CRON_JOBS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not _authorized(authorization):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    task = _cron_tasks()[job]
    send_task_safe(task)
    log.info("Cron job %s enqueued %s", job, task.name)
    return {"job": job, "task": task.name, "status": "enqueued"}
