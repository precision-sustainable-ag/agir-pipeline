"""Final Ceres consistency checks used before publishing an Atlas DB snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.result_sync import (
    CeresResultSyncConfig,
    expected_promoted_inventory_paths,
    verify_promoted_inventory,
)


@dataclass(frozen=True)
class ReconciliationIssue:
    """One condition that makes the canonical Ceres DB unsafe to publish."""

    code: str
    message: str
    run_id: str | None = None


@dataclass(frozen=True)
class ReconciliationReport:
    """Structured result of comparing result sync, run, and inventory state."""

    checked_syncs: int
    ingested_syncs: int
    latest_inventory_run_id: int | None
    expected_promoted_files: int
    issues: tuple[ReconciliationIssue, ...]

    @property
    def ready(self) -> bool:
        return not self.issues


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def reconcile_result_syncs(
    conn,
    config: CeresResultSyncConfig,
) -> ReconciliationReport:
    """Confirm Ceres sync, canonical run, promotion, and inventory state agree."""
    syncs = [dict(row) for row in conn.execute(
        "SELECT * FROM result_syncs ORDER BY registered_at, run_id"
    ).fetchall()]
    issues: list[ReconciliationIssue] = []

    for sync in syncs:
        run_id = str(sync["run_id"])
        status = str(sync["status"])
        if status != "ingested":
            issues.append(
                ReconciliationIssue(
                    code=f"sync_{status}",
                    run_id=run_id,
                    message=(
                        f"result synchronization is {status}"
                        + (
                            f": {sync['error_summary']}"
                            if sync.get("error_summary")
                            else ""
                        )
                    ),
                )
            )
            continue

        stage_run = conn.execute(
            "SELECT batch_id, stage, status FROM stage_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if stage_run is None:
            issues.append(
                ReconciliationIssue(
                    code="stage_run_missing",
                    run_id=run_id,
                    message="ingested result sync has no canonical stage_runs row",
                )
            )
            continue
        conflicts = [
            field
            for field in ("batch_id", "stage", "status")
            if stage_run[field]
            != sync["run_status" if field == "status" else field]
        ]
        if conflicts:
            issues.append(
                ReconciliationIssue(
                    code="stage_run_mismatch",
                    run_id=run_id,
                    message=f"stage_runs disagrees with result_syncs: fields={conflicts}",
                )
            )

    promoted_syncs = [
        sync
        for sync in syncs
        if sync["status"] == "ingested"
        and sync["run_status"] == "success"
        and bool(sync["promotion_succeeded"])
    ]
    inventory_row = conn.execute(
        """
        SELECT run_id, status, ended_at_ts_iso
        FROM inventory_runs
        WHERE endpoint = ? AND site = 'CERES'
          AND storage_domain = ? AND namespace = ?
          AND storage_root = ? AND data_state = ?
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (
            config.ceres_endpoint,
            config.inventory_storage_domain,
            config.inventory_namespace,
            config.inventory_storage_root,
            config.inventory_data_state,
        ),
    ).fetchone()
    inventory_run_id = int(inventory_row["run_id"]) if inventory_row else None

    expected_paths: tuple[Path, ...] = ()
    if promoted_syncs:
        if inventory_row is None:
            issues.append(
                ReconciliationIssue(
                    code="inventory_missing",
                    message="no Ceres developed-images inventory run exists",
                )
            )
        elif inventory_row["status"] != "success":
            issues.append(
                ReconciliationIssue(
                    code="inventory_unsuccessful",
                    message=(
                        f"latest Ceres developed-images inventory run "
                        f"{inventory_row['run_id']} is {inventory_row['status']}"
                    ),
                )
            )
        elif not inventory_row["ended_at_ts_iso"]:
            issues.append(
                ReconciliationIssue(
                    code="inventory_incomplete",
                    message="latest Ceres developed-images inventory has no completion time",
                )
            )
        else:
            latest_ingestion = max(
                _parse_timestamp(str(sync["ingested_at"])) for sync in promoted_syncs
            )
            inventory_ended = _parse_timestamp(str(inventory_row["ended_at_ts_iso"]))
            if inventory_ended < latest_ingestion:
                issues.append(
                    ReconciliationIssue(
                        code="inventory_stale",
                        message=(
                            f"inventory run {inventory_row['run_id']} completed before "
                            "the latest promoted result was ingested"
                        ),
                    )
                )

        try:
            expected_paths = expected_promoted_inventory_paths(conn, config)
            verify_promoted_inventory(conn, config, expected_paths)
        except (OSError, ValueError) as exc:
            issues.append(
                ReconciliationIssue(
                    code="promoted_inventory_mismatch",
                    message=str(exc),
                )
            )

    return ReconciliationReport(
        checked_syncs=len(syncs),
        ingested_syncs=sum(sync["status"] == "ingested" for sync in syncs),
        latest_inventory_run_id=inventory_run_id,
        expected_promoted_files=len(expected_paths),
        issues=tuple(issues),
    )
