"""Readiness polling and staged-input transfer status refresh."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from orchestrator.globus_transfer import TransferPollResult, poll_task
from orchestrator.sqlite_db import get_input_staging_requests, mark_input_staging_status

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkItem:
    """Atomic unit of work for the orchestrator."""

    batch_id: str
    stage: str
    priority: int = 0


class PollingClient:
    """Interface for readiness queries."""

    def next_ready_work(self, limit: int = 1) -> List[WorkItem]:
        """
        Return a list of ready work items, ordered by priority.

        This should query DB readiness views (report.*) via the DB API.
        """
        raise NotImplementedError("PollingClient.next_ready_work is not implemented")


PollFunc = Callable[..., TransferPollResult]


@dataclass(frozen=True)
class StagedInputPollResult:
    """Result of refreshing one staged_inputs row from its Globus task."""

    staging_id: str
    batch_id: str
    stage: str
    previous_status: str
    status: str
    globus_task_id: Optional[str]
    globus_status: Optional[str]
    message: str


def poll_staged_inputs(
    conn,
    *,
    stage: Optional[str] = None,
    statuses: Sequence[str] = ("submitted", "active"),
    limit: int = 50,
    poll_func: PollFunc = poll_task,
) -> List[StagedInputPollResult]:
    """
    Refresh existing staged_inputs rows from Globus task state.

    Only rows with a non-empty ``globus_task_id`` are pollable. Rows without a
    task id are skipped because there is no remote Globus task to inspect.
    """
    rows = get_input_staging_requests(
        conn,
        statuses=statuses,
        stage=stage,
        require_globus_task_id=True,
        limit=limit,
    )
    logger.info(
        "Found %d pollable input-staging row(s) stage=%s statuses=%s limit=%s",
        len(rows),
        stage,
        ",".join(statuses),
        limit,
    )

    results: List[StagedInputPollResult] = []
    for row in rows:
        task_id = row.get("globus_task_id")
        logger.info(
            "Attempting input-staging poll batch_id=%s stage=%s staging_id=%s current_status=%s globus_task_id=%s",
            row["batch_id"],
            row["stage"],
            row["staging_id"],
            row["status"],
            task_id,
        )
        poll_result = poll_func(task_id)
        if poll_result.status in {"failed", "canceled"}:
            logger.warning(
                "Globus poll returned terminal problem batch_id=%s stage=%s staging_id=%s globus_task_id=%s globus_status=%s mapped_status=%s details=%s",
                row["batch_id"],
                row["stage"],
                row["staging_id"],
                task_id,
                poll_result.globus_status,
                poll_result.status,
                poll_result.details,
            )
        else:
            logger.info(
                "Globus poll completed batch_id=%s stage=%s staging_id=%s globus_task_id=%s globus_status=%s mapped_status=%s",
                row["batch_id"],
                row["stage"],
                row["staging_id"],
                task_id,
                poll_result.globus_status,
                poll_result.status,
            )
        updated = mark_input_staging_status(
            conn,
            staging_id=row["staging_id"],
            status=poll_result.status,
            error_summary=(
                poll_result.details
                if poll_result.status in {"failed", "canceled"}
                else None
            ),
        )
        logger.info(
            "Updated input-staging row batch_id=%s stage=%s staging_id=%s status=%s->%s",
            row["batch_id"],
            row["stage"],
            row["staging_id"],
            row["status"],
            updated["status"],
        )
        results.append(
            StagedInputPollResult(
                staging_id=row["staging_id"],
                batch_id=row["batch_id"],
                stage=row["stage"],
                previous_status=row["status"],
                status=updated["status"],
                globus_task_id=task_id,
                globus_status=poll_result.globus_status,
                message=poll_result.details,
            )
        )

    return results


def has_pollable_staged_inputs(
    conn,
    *,
    stage: Optional[str] = None,
    statuses: Sequence[str] = ("submitted", "active"),
) -> bool:
    """Return True when there are staged-input rows that polling can refresh."""
    rows = get_input_staging_requests(
        conn,
        statuses=statuses,
        stage=stage,
        require_globus_task_id=True,
        limit=1,
    )
    return bool(rows)
