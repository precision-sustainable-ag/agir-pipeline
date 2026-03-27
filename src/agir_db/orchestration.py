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
    
    def get_batches_needing_input_staging(
        self,
        limit: int = 100,
        stages: Optional[List[str]] = None,
        min_priority: Optional[int] = None,
    ) -> List[Dict]:
        query = """
            SELECT batch_id, stage, transfer_profile_id, src_lts_ref, dst_staging_ref, priority
            FROM report.batches_needing_input_staging
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

    def request_input_transfer(
        self,
        batch_id: str,
        stage: str,
        transfer_profile_id: str,
        src_lts_ref: str,
        dst_staging_ref: str,
        requested_by: Optional[str] = None,
        priority: int = 100,
        dedupe_key: Optional[str] = None,
        request_ts: Optional[str] = None,
    ) -> Dict:
        row = self.conn.fetch_one(
            """
            SELECT accepted, transfer_id::text AS transfer_id, state, requested_at
            FROM agir_db.request_input_transfer(
                p_batch_id := %s,
                p_stage := %s,
                p_transfer_profile_id := %s,
                p_src_lts_ref := %s,
                p_dst_staging_ref := %s,
                p_requested_by := %s,
                p_priority := %s,
                p_dedupe_key := %s,
                p_request_ts := %s::timestamptz
            )
            """,
            (
                batch_id, stage, transfer_profile_id, src_lts_ref, dst_staging_ref,
                requested_by, priority, dedupe_key, request_ts,
            ),
        )
        return row or {}

    def mark_input_transfer_status(
        self,
        transfer_id: str,
        status: str,
        error_summary: Optional[str] = None,
    ) -> Dict:
        row = self.conn.fetch_one(
            """
            UPDATE logs.transfer_runs
            SET
              status = %s,
              started_at = CASE WHEN %s = 'active' THEN COALESCE(started_at, now()) ELSE started_at END,
              ended_at = CASE WHEN %s IN ('completed', 'failed') THEN COALESCE(ended_at, now()) ELSE ended_at END,
              error_summary = %s
            WHERE transfer_id = %s::uuid
            RETURNING transfer_id::text AS transfer_id, status, requested_at, started_at, ended_at
            """,
            (status, status, status, error_summary, transfer_id),
        )
        return row or {}

    def mark_input_transfer_submitted(
        self,
        transfer_id: str,
        globus_task_id: Optional[str],
        globus_src_endpoint: str,
        globus_dst_endpoint: str,
        globus_label: Optional[str] = None,
        submission_details: Optional[str] = None,
    ) -> Dict:
        """
        Persist Globus submission metadata so transfer requests can be
        reconciled with Globus task state after submission.
        """
        row = self.conn.fetch_one(
            """
            UPDATE logs.transfer_runs
            SET
              status = 'active',
              started_at = COALESCE(started_at, now()),
              globus_task_id = %s,
              globus_src_endpoint = %s,
              globus_dst_endpoint = %s,
              globus_label = %s,
              submission_details = %s
            WHERE transfer_id = %s::uuid
            RETURNING
              transfer_id::text AS transfer_id,
              status,
              globus_task_id,
              started_at
            """,
            (
                globus_task_id,
                globus_src_endpoint,
                globus_dst_endpoint,
                globus_label,
                submission_details,
                transfer_id,
            ),
        )
        return row or {}

    def register_90daydata_index_for_batch(
        self,
        batch_id: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO source.globus_file_index (
                endpoint, site, storage_domain, namespace, storage_root,
                rel_path, full_path, parent_dir, file_name, entry_type, file_ext,
                size_bytes, permissions, checksum, batch_id, batch_state, batch_date,
                data_state, mtime_iso, fname_ts_epoch, fname_ts_iso, created_at_ts_iso
            )
            SELECT
                g.endpoint,
                g.site,
                g.storage_domain,
                '90daydata' AS namespace,
                '/90daydata/dash_agir' AS storage_root,
                g.rel_path,
                ('/90daydata/dash_agir/' || g.rel_path) AS full_path,
                ('/90daydata/dash_agir/' || regexp_replace(g.rel_path, '/[^/]+$', '')) AS parent_dir,
                g.file_name,
                g.entry_type,
                g.file_ext,
                g.size_bytes,
                g.permissions,
                g.checksum,
                g.batch_id,
                g.batch_state,
                g.batch_date,
                g.data_state,
                g.mtime_iso,
                g.fname_ts_epoch,
                g.fname_ts_iso,
                now()
            FROM source.globus_file_index g
            WHERE g.batch_id = %s
              AND g.namespace = 'LTS'
              AND g.entry_type = 'file'
            ON CONFLICT DO NOTHING
            """,
            (batch_id,),
        )
