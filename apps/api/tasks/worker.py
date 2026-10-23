"""Celery worker entrypoint for Vercel Queues.

pyproject.toml declares this module as a queue subscriber:

    [[tool.vercel.subscribers]]
    entrypoint = "apps.api.tasks.worker:app"

At build time Vercel imports it, reads the queues the Celery app declares and
compiles a queue-triggered function that consumes all of them. Every task
module is imported here so each task is registered before that happens; the
`include=` list on the app is only read when a classic `celery worker` boots.

Docker deployments never import this module; they run `celery -A
apps.api.tasks.celery_app worker` as before.
"""
from .celery_app import celery_app
from . import (  # noqa: F401  (imported to register the tasks)
    cleanup_tasks,
    email_tasks,
    reminder_tasks,
    transcode_tasks,
    watermark_tasks,
)

app = celery_app

__all__ = ["app"]
