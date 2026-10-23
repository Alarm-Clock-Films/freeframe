"""The Vercel deployment path: config, queue broker, cron, sandbox offload, SSE limit.

Docker behaviour must be unchanged when none of the Vercel variables are set,
so most tests here check both sides.
"""
import asyncio
import json
import tomllib
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.api.config import Settings, settings
from apps.api.services import event_service, vercel_sandbox

REPO_ROOT = Path(__file__).resolve().parents[3]

BASE = dict(
    redis_url="redis://localhost:6379/0",
    database_url="postgresql://u:p@localhost/db",
    jwt_secret="x" * 32,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


# ── config ────────────────────────────────────────────────────────────────────

def test_marketplace_env_names_are_accepted(monkeypatch):
    for name in ("DATABASE_URL", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgres://neon/db")
    monkeypatch.setenv("KV_URL", "rediss://upstash:6379")
    s = Settings(_env_file=None, jwt_secret="x" * 32)
    assert s.database_url == "postgres://neon/db"
    assert s.redis_url == "rediss://upstash:6379"


def test_the_usual_env_names_win_over_marketplace_ones(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://explicit/db")
    monkeypatch.setenv("POSTGRES_URL", "postgres://neon/db")
    monkeypatch.setenv("REDIS_URL", "redis://explicit:6379")
    monkeypatch.setenv("KV_URL", "rediss://upstash:6379")
    s = Settings(_env_file=None, jwt_secret="x" * 32)
    assert s.database_url == "postgresql://explicit/db"
    assert s.redis_url == "redis://explicit:6379"


def test_off_vercel_the_broker_is_redis_and_streams_are_unbounded(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    s = _settings()
    assert s.running_on_vercel is False
    assert s.celery_broker == BASE["redis_url"]
    assert s.uses_vercel_queues is False
    assert s.sse_stream_limit_seconds == 0.0


def test_on_vercel_the_broker_is_vercel_queues_and_streams_are_bounded(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    s = _settings()
    assert s.celery_broker == "vercel://"
    assert s.uses_vercel_queues is True
    assert 0 < s.sse_stream_limit_seconds < 300


def test_an_explicit_broker_and_stream_limit_win(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    s = _settings(celery_broker_url="redis://elsewhere:6379/1", sse_max_stream_seconds=0)
    assert s.celery_broker == "redis://elsewhere:6379/1"
    assert s.uses_vercel_queues is False
    assert s.sse_stream_limit_seconds == 0


def test_frontend_url_defaults_to_the_vercel_production_domain(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "freeframe.example.com")
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    assert _settings().frontend_url == "https://freeframe.example.com"
    # An explicit value (a custom domain, say) is kept.
    assert _settings(frontend_url="https://review.example.org").frontend_url == "https://review.example.org"


def test_frontend_url_is_untouched_off_vercel(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "freeframe.example.com")
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    assert _settings().frontend_url == "http://localhost:3000"


def test_transcode_runner_is_validated():
    assert _settings(transcode_runner=" Vercel_Sandbox ").transcode_runner == "vercel_sandbox"
    with pytest.raises(ValueError):
        _settings(transcode_runner="lambda")


# ── task dispatch ─────────────────────────────────────────────────────────────

def test_dispatch_is_inline_on_vercel_even_with_another_broker(monkeypatch):
    from apps.api.tasks import celery_app as ca

    monkeypatch.setattr(type(settings), "uses_vercel_queues", property(lambda self: False))
    monkeypatch.setattr(type(settings), "running_on_vercel", property(lambda self: True))
    task = MagicMock()
    with patch.object(ca.threading, "Thread") as thread:
        ca.send_task_safe(task)
    thread.assert_not_called()
    task.delay.assert_called_once_with()


def test_dispatch_is_inline_on_vercel_queues(monkeypatch):
    from apps.api.tasks import celery_app as ca

    monkeypatch.setattr(type(settings), "uses_vercel_queues", property(lambda self: True))
    task = MagicMock()
    with patch.object(ca.threading, "Thread") as thread:
        ca.send_task_safe(task, "a", k=1)
    thread.assert_not_called()
    task.delay.assert_called_once_with("a", k=1)


def test_dispatch_stays_on_a_background_thread_elsewhere(monkeypatch):
    from apps.api.tasks import celery_app as ca

    monkeypatch.setattr(type(settings), "uses_vercel_queues", property(lambda self: False))
    monkeypatch.setattr(type(settings), "running_on_vercel", property(lambda self: False))
    with patch.object(ca.threading, "Thread") as thread:
        ca.send_task_safe(MagicMock(), "a")
    thread.assert_called_once()
    thread.return_value.start.assert_called_once()


def test_worker_entrypoint_registers_every_routed_task():
    from apps.api.tasks.worker import app

    registered = set(app.tasks)
    for name in ("apps.api.tasks.transcode_tasks.process_asset", "apply_watermark",
                 "reap_stale_uploads", "cleanup_soft_deleted", "send_due_date_reminders",
                 "apps.api.tasks.email_tasks.send_magic_code_email"):
        assert name in registered, name
    queues = {q.name for q in app.conf.task_queues}
    assert {"transcoding", "email_high", "email_low", "maintenance", "default"} <= queues


# ── cron ──────────────────────────────────────────────────────────────────────

def test_cron_endpoints_are_hidden_without_a_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", None)
    r = client.get("/internal/cron/reap-stale-uploads", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404


def test_cron_refuses_a_wrong_or_missing_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "s3cret")
    assert client.get("/internal/cron/reap-stale-uploads").status_code == 401
    r = client.get("/internal/cron/reap-stale-uploads", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_cron_rejects_unknown_jobs(client, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "s3cret")
    r = client.get("/internal/cron/rm-rf", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 404


def test_cron_enqueues_the_task(client, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "s3cret")
    with patch("apps.api.routers.cron.send_task_safe") as send:
        r = client.get("/internal/cron/cleanup-soft-deleted", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.json() == {"job": "cleanup-soft-deleted", "task": "cleanup_soft_deleted", "status": "enqueued"}
    send.assert_called_once()
    assert send.call_args.args[0].name == "cleanup_soft_deleted"


def _crontab_string(schedule) -> str:
    """celery crontab -> the five-field string Vercel Cron takes."""
    return " ".join(
        str(x) for x in (schedule._orig_minute, schedule._orig_hour, schedule._orig_day_of_month,
                         schedule._orig_month_of_year, schedule._orig_day_of_week)
    )


def test_vercel_crons_mirror_the_beat_schedule():
    """Every beat job has a Vercel cron at the same time, and nothing else does."""
    from apps.api.routers.cron import CRON_JOBS, _cron_tasks
    from apps.api.tasks.celery_app import celery_app

    beat = celery_app.conf.beat_schedule
    assert set(CRON_JOBS) == set(beat)
    tasks = _cron_tasks()
    for job, entry in beat.items():
        assert tasks[job].name == entry["task"], job

    crons = json.loads((REPO_ROOT / "vercel.json").read_text())["crons"]
    by_job = {c["path"].rsplit("/", 1)[-1]: c for c in crons}
    assert set(by_job) == set(beat)
    for job, entry in beat.items():
        assert by_job[job]["path"] == f"/api/internal/cron/{job}"
        assert by_job[job]["schedule"] == _crontab_string(entry["schedule"]), job


# ── deploy manifests ──────────────────────────────────────────────────────────

def test_vercel_json_routes_api_to_the_python_service():
    cfg = json.loads((REPO_ROOT / "vercel.json").read_text())
    assert cfg["projectSettings"]["framework"] == "services"
    services = cfg["services"]
    assert services["web"]["root"] == "apps/web/"
    assert services["api"]["entrypoint"] == "pyproject.toml"
    # The API's routes have no /api prefix (Traefik strips it in Docker); the
    # service rewrite strips it here.
    assert services["api"]["rewrites"] == [{"source": "/api/:path(.*)?", "destination": "/:path"}]
    first, last = cfg["rewrites"][0], cfg["rewrites"][-1]
    assert first["destination"] == {"service": "api"} and first["source"].startswith("/api/")
    assert last == {"source": "/(.*)", "destination": {"service": "web"}}


def _requirement_names(lines) -> dict[str, str]:
    out = {}
    for line in lines:
        line = line.split("#")[0].strip()
        if line:
            name = line.split("==")[0].split("[")[0].strip().lower()
            out[name] = line
    return out


def test_pyproject_dependencies_match_requirements_txt():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    vercel_deps = _requirement_names(pyproject["project"]["dependencies"])
    docker_deps = _requirement_names((REPO_ROOT / "apps/api/requirements.txt").read_text().splitlines())
    for name, pin in docker_deps.items():
        assert vercel_deps.get(name) == pin, f"{name}: pyproject.toml has {vercel_deps.get(name)!r}, requirements.txt {pin!r}"
    assert "vercel" in vercel_deps


def test_pyproject_entrypoints_resolve():
    import importlib

    tool = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["vercel"]
    entrypoints = [tool["entrypoint"]] + [s["entrypoint"] for s in tool["subscribers"]]
    for ep in entrypoints:
        module, attr = ep.split(":")
        assert getattr(importlib.import_module(module), attr) is not None, ep


def test_vercel_build_migrates_production_only_by_default():
    from apps.api.scripts.vercel_build import should_migrate

    assert should_migrate({"VERCEL_ENV": "production"}) is True
    assert should_migrate({"VERCEL_ENV": "preview"}) is False
    assert should_migrate({}) is False
    assert should_migrate({"VERCEL_ENV": "preview", "FREEFRAME_MIGRATE_ON_BUILD": "1"}) is True
    assert should_migrate({"VERCEL_ENV": "production", "FREEFRAME_MIGRATE_ON_BUILD": "0"}) is False


# ── SSE ───────────────────────────────────────────────────────────────────────

class _IdlePubSub:
    async def subscribe(self, channel): pass
    async def unsubscribe(self, channel): pass
    async def aclose(self): pass

    async def get_message(self, ignore_subscribe_messages=False, timeout=0.0):
        await asyncio.sleep(min(timeout or 0, 0.05))
        return None


def test_a_bounded_stream_ends_cleanly_with_a_retry_hint(monkeypatch):
    monkeypatch.setattr(event_service, "_get_redis", lambda: SimpleNamespace(pubsub=_IdlePubSub))

    async def authorize():
        return None

    async def collect():
        out = []
        async for chunk in event_service.event_stream(
            "p", authorize, max_auth_age_seconds=1, recheck_seconds=30,
            max_duration_seconds=0.2,
        ):
            out.append(chunk)
        return out

    chunks = asyncio.run(asyncio.wait_for(collect(), timeout=5))
    assert chunks[-1] == "retry: 1000\n\n"


# ── sandbox offload ───────────────────────────────────────────────────────────

def test_offload_only_with_the_sandbox_runner_and_never_inside_one(monkeypatch):
    monkeypatch.delenv(vercel_sandbox.IN_SANDBOX_ENV, raising=False)
    monkeypatch.setattr(settings, "transcode_runner", "inline")
    assert vercel_sandbox.should_offload() is False
    monkeypatch.setattr(settings, "transcode_runner", "vercel_sandbox")
    assert vercel_sandbox.should_offload() is True
    monkeypatch.setenv(vercel_sandbox.IN_SANDBOX_ENV, "1")
    assert vercel_sandbox.should_offload() is False


def test_sandbox_env_forwards_app_config_only():
    env = vercel_sandbox.sandbox_env({
        "DATABASE_URL": "postgresql://db", "KV_URL": "rediss://kv", "JWT_SECRET": "j",
        "S3_BUCKET": "b", "TRANSCODER_HDR": "auto",
        "VERCEL": "1", "VERCEL_OIDC_TOKEN": "tok", "AWS_SECRET_ACCESS_KEY": "lambda-creds",
        "CELERY_BROKER_URL": "vercel://", "TRANSCODE_RUNNER": "vercel_sandbox", "HOME": "/root",
    })
    for kept in ("DATABASE_URL", "KV_URL", "JWT_SECRET", "S3_BUCKET", "TRANSCODER_HDR"):
        assert kept in env, kept
    for dropped in ("VERCEL", "VERCEL_OIDC_TOKEN", "AWS_SECRET_ACCESS_KEY", "HOME"):
        assert dropped not in env, dropped
    assert env[vercel_sandbox.IN_SANDBOX_ENV] == "1"
    assert env["TRANSCODE_RUNNER"] == "inline"
    assert env["CELERY_BROKER_URL"] == "memory://"
    assert env["FRONTEND_URL"] == settings.frontend_url


class _FakeProcess:
    def __init__(self, finishes_after: int):
        self._polls = finishes_after
        self.returncode = None

    def refresh(self):
        self._polls -= 1
        if self._polls <= 0:
            self.returncode = 0
        return self


@pytest.fixture
def fake_sandbox(monkeypatch):
    monkeypatch.setattr(settings, "vercel_sandbox_image", "freeframe-worker:latest")
    monkeypatch.setattr(vercel_sandbox, "_POLL_SECONDS", 0)
    monkeypatch.setattr(vercel_sandbox, "_resources", lambda: "resources")
    sandbox = MagicMock()
    sandbox.name = "sbx_1"
    client = MagicMock()
    client.create_sandbox.return_value = sandbox
    monkeypatch.setattr(vercel_sandbox, "_client", lambda: client)
    return SimpleNamespace(client=client, sandbox=sandbox)


def test_run_in_sandbox_runs_the_task_and_stops_the_sandbox(fake_sandbox, monkeypatch):
    monkeypatch.setattr(vercel_sandbox, "_seconds_left_to_wait", lambda: 60)
    fake_sandbox.sandbox.create_process.return_value = _FakeProcess(finishes_after=2)

    assert vercel_sandbox.run_in_sandbox("process_asset", ["a", "v"]) == 0

    kwargs = fake_sandbox.client.create_sandbox.call_args.kwargs
    assert kwargs["image"] == "freeframe-worker:latest"
    assert kwargs["destroy"] is False
    assert kwargs["env"][vercel_sandbox.IN_SANDBOX_ENV] == "1"
    cmd, args = fake_sandbox.sandbox.create_process.call_args.args
    assert cmd == "python" and args[:2] == ["-m", "apps.api.tasks.sandbox_runner"]
    assert json.loads(args[2]) == {"task": "process_asset", "args": ["a", "v"], "kwargs": {}}
    fake_sandbox.sandbox.stop.assert_called_once()
    fake_sandbox.client.close.assert_called_once()


def test_run_in_sandbox_leaves_a_long_task_running(fake_sandbox, monkeypatch):
    monkeypatch.setattr(vercel_sandbox, "_seconds_left_to_wait", lambda: 0)
    fake_sandbox.sandbox.create_process.return_value = _FakeProcess(finishes_after=10**6)

    assert vercel_sandbox.run_in_sandbox("process_asset", ["a", "v"]) is None
    fake_sandbox.sandbox.stop.assert_not_called()
    fake_sandbox.client.close.assert_called_once()


def test_run_in_sandbox_needs_an_image(monkeypatch):
    monkeypatch.setattr(settings, "vercel_sandbox_image", None)
    with pytest.raises(vercel_sandbox.SandboxUnavailable):
        vercel_sandbox.run_in_sandbox("process_asset", ["a", "v"])


def test_process_asset_offloads_without_touching_the_database(monkeypatch):
    from apps.api.tasks import transcode_tasks

    monkeypatch.setattr(transcode_tasks, "should_offload", lambda: True)
    session = MagicMock()
    monkeypatch.setattr(transcode_tasks, "SessionLocal", session)
    with patch.object(transcode_tasks, "run_in_sandbox") as run:
        aid, vid = str(uuid.uuid4()), str(uuid.uuid4())
        transcode_tasks.process_asset.run(aid, vid)
    run.assert_called_once_with("process_asset", [aid, vid])
    session.assert_not_called()


def test_apply_watermark_offloads(monkeypatch):
    from apps.api.tasks import watermark_tasks

    monkeypatch.setattr(vercel_sandbox, "should_offload", lambda: True)
    with patch.object(vercel_sandbox, "run_in_sandbox") as run, \
         patch.object(watermark_tasks, "SessionLocal") as session:
        watermark_tasks.apply_watermark.run("a", "txt", "center", 0.5, None)
    run.assert_called_once_with("apply_watermark", ["a", "txt", "center", 0.5, None])
    session.assert_not_called()


def test_sandbox_runner_refuses_unknown_tasks():
    from apps.api.tasks.sandbox_runner import run

    assert run({"task": "cleanup_soft_deleted", "args": []}) == 2


def test_sandbox_runner_runs_the_task_eagerly(monkeypatch):
    from apps.api.tasks import sandbox_runner, transcode_tasks
    from apps.api.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app.conf, "task_always_eager", False)
    result = MagicMock()
    result.failed.return_value = False
    with patch.object(transcode_tasks.process_asset, "apply", return_value=result) as apply:
        assert sandbox_runner.run({"task": "process_asset", "args": ["a", "v"]}) == 0
    apply.assert_called_once_with(args=["a", "v"], kwargs={})

    result.failed.return_value = True
    with patch.object(transcode_tasks.process_asset, "apply", return_value=result):
        assert sandbox_runner.run({"task": "process_asset", "args": ["a", "v"]}) == 1
