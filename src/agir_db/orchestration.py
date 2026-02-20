"""
Minimal orchestration DB API for Phase 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional
from uuid import UUID

from .connection import ConnectionManager
from .exceptions import ValidationError


class OrchestrationManager:
    def __init__(self, connection: ConnectionManager):
        self.conn = connection

    def get_ready_work(
        self,
        limit: int = 100,
        stages: Optional[List[str]] = None,
        min_priority: Optional[int] = None,
    ) -> List[Dict]:
        query = """
            SELECT batch_id, stage, priority, resource_profile_id, config_id, staging_input_ref
            FROM report.ready_work
            WHERE (%s IS NULL OR stage = ANY(%s))
              AND (%s IS NULL OR priority >= %s)
            ORDER BY priority DESC, batch_id ASC
            LIMIT %s
        """
        params = (
            stages if stages else None,
            stages if stages else None,
            min_priority,
            min_priority,
            limit,
        )
        return self.conn.fetch_all(query, params)

    def claim_stage_lease(
        self,
        batch_id: str,
        stage: str,
        orchestrator_id: str,
        ttl_seconds: int,
        attempt: Optional[int] = None,
    ) -> Dict:
        row = self.conn.fetch_one(
            """
            SELECT claimed, lease_id, batch_id, stage, expires_at, attempt, job_workdir_policy
            FROM ops.claim_stage_lease(
                p_batch_id := %s,
                p_stage := %s,
                p_orchestrator_id := %s,
                p_ttl_seconds := %s,
                p_attempt := %s
            )
            """,
            (batch_id, stage, orchestrator_id, ttl_seconds, attempt),
        )
        return row or {}

    def release_stage_lease(
        self,
        lease_id: str,
        orchestrator_id: str,
        release_reason: str,
        released_at: Optional[str] = None,
    ) -> Dict:
        row = self.conn.fetch_one(
            """
            SELECT released, lease_id, released_at, release_reason
            FROM ops.release_stage_lease(
                p_lease_id := %s::uuid,
                p_orchestrator_id := %s,
                p_release_reason := %s,
                p_released_at := %s::timestamptz
            )
            """,
            (lease_id, orchestrator_id, release_reason, released_at),
        )
        return row or {}

    def ingest_run_report(self, run_report_path: str) -> Dict:
        """
        Minimal Phase 1 ingest:
        - read JSON
        - light required-field validation
        - upsert into logs.stage_runs by run_id
        """
        path = Path(run_report_path)
        report = json.loads(path.read_text())

        required = [
            "run_id",
            "batch_id",
            "stage",
            "attempt",
            "status",
            "started_at",
            "ended_at",
        ]
        missing = [k for k in required if k not in report]
        if missing:
            raise ValidationError(f"run_report missing required fields: {missing}")

        if report["status"] not in {"success", "partial", "failed"}:
            raise ValidationError("status must be one of: success, partial, failed")

        _ = UUID(str(report["run_id"]))  # validate UUID

        row = self.conn.fetch_one(
            """
            INSERT INTO logs.stage_runs (
                run_id, batch_id, stage, attempt, status, exit_code,
                started_at, ended_at, run_report_ref, output_ref, updated_at
            )
            VALUES (
                %s::uuid, %s, %s, %s, %s, %s,
                %s::timestamptz, %s::timestamptz, %s, %s, now()
            )
            ON CONFLICT (run_id) DO UPDATE
            SET
                batch_id = EXCLUDED.batch_id,
                stage = EXCLUDED.stage,
                attempt = EXCLUDED.attempt,
                status = EXCLUDED.status,
                exit_code = EXCLUDED.exit_code,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                run_report_ref = EXCLUDED.run_report_ref,
                output_ref = EXCLUDED.output_ref,
                updated_at = now()
            RETURNING run_id::text AS run_id, batch_id, stage, status
            """,
            (
                report["run_id"],
                report["batch_id"],
                report["stage"],
                int(report["attempt"]),
                report["status"],
                report.get("exit_code"),
                report["started_at"],
                report["ended_at"],
                str(path),
                report.get("output_ref"),
            ),
        )
        return row or {}
