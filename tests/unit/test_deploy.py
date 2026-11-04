"""flows/deploy.py: the deployment objects are built without a Prefect server (week-2 T4, decision W2-2)."""

from __future__ import annotations

import pytest
from prefect.client.schemas.objects import ConcurrencyLimitStrategy
from prefect.client.schemas.schedules import CronSchedule
from prefect.deployments.runner import RunnerDeployment

from flows import deploy


def _by_name(name: str) -> RunnerDeployment:
    found = [d for d in deploy.build_deployments() if d.name == name]
    assert len(found) == 1, f"expected exactly one {name!r} deployment, got {len(found)}"
    return found[0]


def test_ingest_deployment_schedule() -> None:
    d = _by_name("ingest")
    assert d.flow_name == "ingest"  # `prefect deployment ls` shows it as ingest/ingest
    assert d.schedules is not None
    assert len(d.schedules) == 1
    schedule = d.schedules[0].schedule
    assert isinstance(schedule, CronSchedule)
    assert schedule.cron == "*/15 * * * *"
    assert schedule.timezone == "Asia/Seoul"
    assert d.schedules[0].active is True


def test_ingest_deployment_runs_one_at_a_time() -> None:
    d = _by_name("ingest")
    assert d.concurrency_limit == 1
    assert d.concurrency_options is not None
    assert d.concurrency_options.collision_strategy == ConcurrencyLimitStrategy.CANCEL_NEW


def test_ingest_deployment_points_at_the_flow_in_the_worker_image() -> None:
    d = _by_name("ingest")
    assert d.work_pool_name == "default"
    assert d.entrypoint == "flows/ingest_flow.py:ingest_flow"
    assert d.storage is not None  # from_source: the worker runs the code at the repo path, not a pickled copy


def test_only_ingest_is_deployed_in_week_2_t4() -> None:
    assert [d.name for d in deploy.build_deployments()] == ["ingest"]  # extract arrives with T13


def test_main_registers_every_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[str] = []

    def fake_apply(self: RunnerDeployment, *args: object, **kwargs: object) -> str:
        applied.append(self.name)
        return "00000000-0000-0000-0000-000000000000"

    monkeypatch.setattr(RunnerDeployment, "apply", fake_apply)
    assert deploy.register() == 1
    assert applied == ["ingest"]
