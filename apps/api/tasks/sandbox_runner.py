"""Run one task inside a Vercel Sandbox: the other half of services/vercel_sandbox.py.

    python -m apps.api.tasks.sandbox_runner '{"task": "...", "args": [...], "kwargs": {...}}'

The sandbox environment sets FREEFRAME_IN_SANDBOX=1, so the task does its work
here instead of offloading again. It runs eagerly, in this process: Celery's
own retry loop still applies (an eager retry runs the task again at once), and
anything it dispatches runs inline rather than on a queue.

Exit code 0 means the task returned; 1 means it raised.
"""
import json
import logging
import sys

log = logging.getLogger("apps.api.sandbox_runner")

# Only tasks that are offloaded may be run this way.
RUNNABLE_TASKS = {
    "process_asset": ("apps.api.tasks.transcode_tasks", "process_asset"),
    "backfill_media_metadata": ("apps.api.tasks.transcode_tasks", "backfill_media_metadata"),
    "apply_watermark": ("apps.api.tasks.watermark_tasks", "apply_watermark"),
}


def run(payload: dict) -> int:
    import importlib

    from .celery_app import celery_app

    name = payload.get("task")
    if name not in RUNNABLE_TASKS:
        log.error("Refusing to run unknown task %r", name)
        return 2
    module_name, attr = RUNNABLE_TASKS[name]
    task = getattr(importlib.import_module(module_name), attr)

    celery_app.conf.task_always_eager = True
    result = task.apply(args=payload.get("args") or [], kwargs=payload.get("kwargs") or {})
    if result.failed():
        log.error("Task %s failed: %r", name, result.result)
        return 1
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    return run(json.loads(argv[1]))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
