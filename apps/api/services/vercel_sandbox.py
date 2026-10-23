"""Run transcoding work in a Vercel Sandbox (TRANSCODE_RUNNER=vercel_sandbox).

A Vercel Function has no FFmpeg or ImageMagick and runs for at most a few
minutes, while a transcode needs both and can take hours. With this runner the
Celery task that Vercel Queues delivers does not do the work itself. It starts
a sandbox from the image built from apps/api/Dockerfile.prod (which carries
FFmpeg, ImageMagick and this code) and runs the same task there with
`python -m apps.api.tasks.sandbox_runner`.

The worker waits for the sandbox for as long as its own invocation allows and
stops it if the task has finished. A longer transcode keeps running after the
worker returns and is ended by the sandbox's own time limit. Its result reaches
the app the usual way: the task writes the database and publishes SSE events.

Nothing here runs unless TRANSCODE_RUNNER=vercel_sandbox, and the `vercel`
SDK is imported only then, so Docker deployments do not need it installed.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import Settings, settings

log = logging.getLogger(__name__)

# Set inside the sandbox, so the task runs there instead of offloading again.
IN_SANDBOX_ENV = "FREEFRAME_IN_SANDBOX"

# Leave the worker this long to stop the sandbox and return before its own
# deadline, and use it when the runtime gives no deadline at all.
_DEADLINE_MARGIN_SECONDS = 20.0
_FALLBACK_WAIT_SECONDS = 240.0
_POLL_SECONDS = 5.0

# Not forwarded into the sandbox. VERCEL=1 would make the task think it runs in
# a Vercel Function (Vercel Queues broker, short SSE streams) and the OIDC token
# is a credential the task does not need.
_NEVER_FORWARD = {"VERCEL", "VERCEL_OIDC_TOKEN", "CELERY_BROKER_URL", "TRANSCODE_RUNNER"}

# Variables the transcoder reads with os.getenv rather than through Settings.
# Deliberately not AWS_*: inside a function those include the platform's own
# temporary credentials.
_EXTRA_FORWARD_PREFIXES = ("TRANSCODER_",)


class SandboxUnavailable(RuntimeError):
    """The sandbox runner is selected but cannot start a sandbox."""


def should_offload() -> bool:
    """True when a transcoding task should hand its work to a sandbox."""
    return settings.transcode_runner == "vercel_sandbox" and os.environ.get(IN_SANDBOX_ENV) != "1"


def _settings_env_names() -> set[str]:
    """Every environment variable name Settings reads, aliases included."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name.upper())
        alias = field.validation_alias
        choices = getattr(alias, "choices", None)
        if choices:
            names.update(str(c).upper() for c in choices)
        elif isinstance(alias, str):
            names.add(alias.upper())
    return names


def sandbox_env(environ: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The environment the task gets inside the sandbox.

    Only configuration the app reads is forwarded, never the whole function
    environment. The broker is in-memory and tasks run eagerly there: anything
    the task dispatches runs inline instead of going back onto a queue the
    sandbox has no credentials for.
    """
    environ = dict(os.environ if environ is None else environ)
    wanted = _settings_env_names()
    env = {
        k: v for k, v in environ.items()
        if k not in _NEVER_FORWARD
        and (k in wanted or k.startswith(_EXTRA_FORWARD_PREFIXES))
    }
    env.update({
        # The resolved value: on Vercel it may come from the project's domain,
        # whose variable is not forwarded.
        "FRONTEND_URL": settings.frontend_url,
        IN_SANDBOX_ENV: "1",
        "TRANSCODE_RUNNER": "inline",
        "CELERY_BROKER_URL": "memory://",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def _seconds_left_to_wait() -> float:
    """How long this invocation may wait for the sandbox before returning."""
    deadline: Optional[datetime] = None
    try:
        from vercel.functions import get_deadline
        deadline = get_deadline()
    except Exception:  # SDK missing or no runtime deadline
        deadline = None
    if deadline is None:
        return _FALLBACK_WAIT_SECONDS
    left = (deadline - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, left - _DEADLINE_MARGIN_SECONDS)


def _client():
    try:
        from vercel.sandbox.sync import SyncSandboxClient
    except ImportError as exc:  # pragma: no cover - depends on install
        raise SandboxUnavailable(
            "TRANSCODE_RUNNER=vercel_sandbox needs the `vercel` package "
            "(it is a dependency in pyproject.toml for Vercel builds)."
        ) from exc
    return SyncSandboxClient.create()


def _resources():
    from vercel.sandbox.sync import SandboxResources
    return SandboxResources(vcpus=settings.vercel_sandbox_vcpus)


def run_in_sandbox(task_name: str, args: list[Any], kwargs: Optional[dict[str, Any]] = None) -> Optional[int]:
    """Run task `task_name` with these arguments in a new sandbox.

    Returns the task process's exit code if it finished while this invocation
    could still wait, or None if it is still running (it then ends on its own
    or at the sandbox's time limit). Raises if the sandbox cannot be started,
    so the queue delivers the message again.
    """
    if not settings.vercel_sandbox_image:
        raise SandboxUnavailable(
            "TRANSCODE_RUNNER=vercel_sandbox requires VERCEL_SANDBOX_IMAGE: the image "
            "built from apps/api/Dockerfile.prod, pushed to the Vercel Container "
            "Registry. See docs/deployment-vercel.md."
        )

    payload = json.dumps({"task": task_name, "args": list(args), "kwargs": dict(kwargs or {})})
    limit = timedelta(minutes=settings.vercel_sandbox_timeout_minutes)
    client = _client()
    try:
        sandbox = client.create_sandbox(
            image=settings.vercel_sandbox_image,
            env=sandbox_env(),
            execution_time_limit=limit,
            resources=_resources(),
            tags={"app": "freeframe", "task": task_name},
            # Outlive this invocation: a long transcode keeps going after the
            # worker returns, and the time limit above is what ends it.
            destroy=False,
        )
        log.info("Started sandbox %s for %s%s", sandbox.name, task_name, list(args))
        process = sandbox.create_process(
            "python",
            ["-m", "apps.api.tasks.sandbox_runner", payload],
            cwd=settings.vercel_sandbox_workdir,
            kill_after=limit - timedelta(seconds=30) if limit > timedelta(minutes=1) else limit,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        wait_until = time.monotonic() + _seconds_left_to_wait()
        while True:
            process.refresh()
            if process.returncode is not None:
                code = process.returncode
                log.info("Sandbox %s finished %s with exit code %s", sandbox.name, task_name, code)
                try:
                    sandbox.stop()
                except Exception:
                    log.warning("Could not stop sandbox %s; its time limit will", sandbox.name, exc_info=True)
                return code
            if time.monotonic() >= wait_until:
                log.info("Sandbox %s still running %s; leaving it to finish", sandbox.name, task_name)
                return None
            time.sleep(_POLL_SECONDS)
    finally:
        try:
            client.close()
        except Exception:
            pass
