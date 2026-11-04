"""Register the Prefect deployments (tech-phase0.md §6.1, decision W2-2). Run inside the worker: `make deploy`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID

from prefect import Flow, flow
from prefect.client.schemas.objects import ConcurrencyLimitConfig, ConcurrencyLimitStrategy
from prefect.client.schemas.schedules import CronSchedule
from prefect.deployments.runner import RunnerDeployment

# The repo root: /app inside the worker image, the checkout on a host. The process worker runs the flow from here.
ROOT = Path(__file__).resolve().parents[1]

WORK_POOL = "default"
INGEST_CRON = "*/15 * * * *"
TIMEZONE = "Asia/Seoul"
# One run at a time, and a run that starts while another is going is cancelled instead of queued (tech §6.1).
ONE_AT_A_TIME = ConcurrencyLimitConfig(limit=1, collision_strategy=ConcurrencyLimitStrategy.CANCEL_NEW)


def build_deployments(source: Path = ROOT) -> list[RunnerDeployment]:
    """The deployment objects, built without a Prefect server (registering them is `register`)."""
    # Prefect types these sync-or-async (`async_dispatch`); called from sync code they return the plain objects.
    ingest_flow = cast(Flow[..., Any], flow.from_source(source, entrypoint="flows/ingest_flow.py:ingest_flow"))
    extract_flow = cast(Flow[..., Any], flow.from_source(source, entrypoint="flows/extract_flow.py:extract_flow"))
    ingest = cast(
        RunnerDeployment,
        ingest_flow.to_deployment(
            name="ingest",
            schedules=[CronSchedule(cron=INGEST_CRON, timezone=TIMEZONE)],
            concurrency_limit=ONE_AT_A_TIME,
            work_pool_name=WORK_POOL,
        ),
    )
    # No schedule: `ingest_flow` starts it (decision W2-12), and it does nothing while EXTRACT_ENABLED is off.
    extract = cast(
        RunnerDeployment,
        extract_flow.to_deployment(name="extract", concurrency_limit=ONE_AT_A_TIME, work_pool_name=WORK_POOL),
    )
    return [ingest, extract]


def register(source: Path = ROOT) -> int:
    """Create or update every deployment on the Prefect server (PREFECT_API_URL). Returns how many."""
    deployments = build_deployments(source)
    for d in deployments:
        deployment_id = cast(UUID, d.apply())
        print(f"deployed {d.flow_name}/{d.name} ({deployment_id})")
    return len(deployments)


if __name__ == "__main__":
    register()
