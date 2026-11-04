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


def test_extract_deployment_has_no_schedule() -> None:
    d = _by_name("extract")
    assert d.flow_name == "extract"  # `prefect deployment ls` shows it as extract/extract (what ingest triggers, W2-12)
    assert not d.schedules  # started by ingest_flow only, never by cron


def test_extract_deployment_runs_one_at_a_time() -> None:
    d = _by_name("extract")
    assert d.concurrency_limit == 1
    assert d.concurrency_options is not None
    assert d.concurrency_options.collision_strategy == ConcurrencyLimitStrategy.CANCEL_NEW


def test_extract_deployment_points_at_the_flow_in_the_worker_image() -> None:
    d = _by_name("extract")
    assert d.work_pool_name == "default"
    assert d.entrypoint == "flows/extract_flow.py:extract_flow"
    assert d.storage is not None


def test_ingest_and_extract_are_the_deployments() -> None:
    assert [d.name for d in deploy.build_deployments()] == ["ingest", "extract"]


def test_main_registers_every_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[str] = []

    def fake_apply(self: RunnerDeployment, *args: object, **kwargs: object) -> str:
        applied.append(self.name)
        return "00000000-0000-0000-0000-000000000000"

    monkeypatch.setattr(RunnerDeployment, "apply", fake_apply)
    assert deploy.register() == 2
    assert applied == ["ingest", "extract"]
