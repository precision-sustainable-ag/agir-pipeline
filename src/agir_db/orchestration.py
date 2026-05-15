"""
Minimal orchestration DB API for Phase 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional
from uuid import UUID

from orchestrator.batch_list import make_window_key

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
        window_key: str = "",
    ) -> Dict:
        row = self.conn.fetch_one(
            """
            SELECT claimed, lease_id, batch_id, stage, expires_at, attempt, job_workdir_policy
            FROM ops.claim_stage_lease(
                p_batch_id := %s,
                p_stage := %s,
                p_orchestrator_id := %s,
                p_ttl_seconds := %s,
                p_attempt := %s,
                p_window_key := %s
            )
            """,
            (batch_id, stage, orchestrator_id, ttl_seconds, attempt, window_key),
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

    def request_windowed_input_transfer(
        self,
        batch_id: str,
        stage: str,
        start_epoch: int,
        end_epoch: int,
        src_lts_ref: str,
        dst_staging_ref: str,
        requested_by: Optional[str] = None,
        priority: int = 100,
    ) -> Dict:
        """Request a transfer for a specific epoch-windowed subset of a batch.

        Unlike request_input_transfer (which uses a batch-level already-staged
        check), this method checks whether the specific windowed files are
        already on 90daydata so that different time windows of the same batch
        can be transferred and processed independently.

        Returns the same shape as request_input_transfer:
          {accepted, transfer_id, state, requested_at}
        """
        # Check whether all LTS files for this window are already on 90daydata
        lts_row = self.conn.fetch_one(
            """
            SELECT COUNT(*) AS n FROM source.globus_file_index
            WHERE batch_id = %s AND namespace = 'LTS'
              AND data_state = 'semifield-upload' AND entry_type = 'file'
              AND lower(file_ext) = 'raw'
              AND fname_ts_epoch >= %s AND fname_ts_epoch <= %s
            """,
            (batch_id, start_epoch, end_epoch),
        )
        staged_row = self.conn.fetch_one(
            """
            SELECT COUNT(*) AS n FROM source.globus_file_index
            WHERE batch_id = %s AND namespace = '90daydata'
              AND data_state = 'semifield-upload' AND entry_type = 'file'
              AND lower(file_ext) = 'raw'
              AND fname_ts_epoch >= %s AND fname_ts_epoch <= %s
            """,
            (batch_id, start_epoch, end_epoch),
        )
        lts_count = int((lts_row or {}).get("n", 0))
        staged_count = int((staged_row or {}).get("n", 0))
        if lts_count > 0 and staged_count >= lts_count:
            return {
                "accepted": True,
                "transfer_id": None,
                "state": "already_completed",
                "requested_at": None,
            }

        # Check for an in-flight transfer for this exact window
        dedupe_key = f"input_stage:{batch_id}:{stage}:{make_window_key(start_epoch, end_epoch)}"
        existing = self.conn.fetch_one(
            """
            SELECT transfer_id::text AS transfer_id, status, requested_at
            FROM logs.transfer_runs
            WHERE dedupe_key = %s AND status IN ('requested', 'active')
            ORDER BY requested_at DESC LIMIT 1
            """,
            (dedupe_key,),
        )
        if existing:
            return {
                "accepted": True,
                "transfer_id": existing["transfer_id"],
                "state": "already_active",
                "requested_at": existing["requested_at"],
            }

        row = self.conn.fetch_one(
            """
            INSERT INTO logs.transfer_runs (
                transfer_id, direction, batch_id, stage, window_key, transfer_profile_id,
                src_lts_ref, dst_staging_ref, priority, requested_by,
                dedupe_key, status, requested_at
            )
            VALUES (
                ops.uuid_v4(), 'input_stage', %s, %s, %s, 'juno_to_ceres_targeted',
                %s, %s, %s, %s, %s, 'requested', now()
            )
            RETURNING transfer_id::text AS transfer_id, status, requested_at
            """,
            (
                batch_id, stage, make_window_key(start_epoch, end_epoch), src_lts_ref, dst_staging_ref,
                priority, requested_by, dedupe_key,
            ),
        )
        return {
            "accepted": True,
            "transfer_id": (row or {}).get("transfer_id"),
            "state": "created",
            "requested_at": (row or {}).get("requested_at"),
        }

    def register_windowed_90daydata_files(
        self,
        batch_id: str,
        start_epoch: int,
        end_epoch: int,
    ) -> None:
        """Register only the epoch-windowed files as present on 90daydata.

        Use instead of register_90daydata_index_for_batch for windowed
        transfers so that files outside the window are not falsely marked
        as staged.
        """
        self.conn.execute(
            """
            INSERT INTO source.globus_file_index (
                endpoint, site, storage_domain, namespace, storage_root,
                rel_path, full_path, parent_dir, file_name, entry_type, file_ext,
                size_bytes, permissions, checksum, batch_id, batch_state, batch_date,
                data_state, mtime_iso, fname_ts_epoch, fname_ts_iso, created_at_ts_iso
            )
            SELECT
                g.endpoint, g.site, g.storage_domain,
                '90daydata' AS namespace,
                '/90daydata/dash_agir' AS storage_root,
                g.rel_path,
                ('/90daydata/dash_agir/' || g.rel_path) AS full_path,
                ('/90daydata/dash_agir/' || regexp_replace(g.rel_path, '/[^/]+$', '')) AS parent_dir,
                g.file_name, g.entry_type, g.file_ext,
                g.size_bytes, g.permissions, g.checksum,
                g.batch_id, g.batch_state, g.batch_date, g.data_state,
                g.mtime_iso, g.fname_ts_epoch, g.fname_ts_iso, now()
            FROM source.globus_file_index g
            WHERE g.batch_id = %s
              AND g.namespace = 'LTS'
              AND g.entry_type = 'file'
              AND g.fname_ts_epoch >= %s
              AND g.fname_ts_epoch <= %s
            ON CONFLICT DO NOTHING
            """,
            (batch_id, start_epoch, end_epoch),
        )

    def are_windowed_inputs_staged(
        self,
        batch_id: str,
        start_epoch: int,
        end_epoch: int,
    ) -> bool:
        """Return True if all LTS files for this epoch window are on 90daydata."""
        lts_row = self.conn.fetch_one(
            """
            SELECT COUNT(*) AS n FROM source.globus_file_index
            WHERE batch_id = %s AND namespace = 'LTS'
              AND data_state = 'semifield-upload' AND entry_type = 'file'
              AND lower(file_ext) = 'raw'
              AND fname_ts_epoch >= %s AND fname_ts_epoch <= %s
            """,
            (batch_id, start_epoch, end_epoch),
        )
        staged_row = self.conn.fetch_one(
            """
            SELECT COUNT(*) AS n FROM source.globus_file_index
            WHERE batch_id = %s AND namespace = '90daydata'
              AND data_state = 'semifield-upload' AND entry_type = 'file'
              AND lower(file_ext) = 'raw'
              AND fname_ts_epoch >= %s AND fname_ts_epoch <= %s
            """,
            (batch_id, start_epoch, end_epoch),
        )
        lts_count = int((lts_row or {}).get("n", 0))
        staged_count = int((staged_row or {}).get("n", 0))
        return lts_count > 0 and staged_count >= lts_count

    def get_lease_window_key(self, lease_id: str) -> str:
        """Return the window_key stored on a lease row, or '' if not found."""
        row = self.conn.fetch_one(
            "SELECT window_key FROM logs.stage_leases WHERE lease_id = %s::uuid",
            (lease_id,),
        )
        return (row or {}).get("window_key", "") or ""

    def ingest_run_report(self, run_report_path: str, window_key: str = "") -> Dict:
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
            "status",
            "started_at",
            "ended_at",
        ]
        missing = [k for k in required if k not in report]
        if missing:
            raise ValidationError(f"run_report missing required fields: {missing}")
        
                # attempt is tracked on the lease, not written by the stage CLI; default to 1
        if "attempt" not in report:
            report["attempt"] = 1

        if report["status"] not in {"success", "partial", "failed"}:
            raise ValidationError("status must be one of: success, partial, failed")

        _ = UUID(str(report["run_id"]))  # validate UUID

        row = self.conn.fetch_one(
            """
            INSERT INTO logs.stage_runs (
                run_id, batch_id, stage, window_key, attempt, status, exit_code,
                started_at, ended_at, run_report_ref, output_ref, updated_at
            )
            VALUES (
                %s::uuid, %s, %s, %s, %s, %s, %s,
                %s::timestamptz, %s::timestamptz, %s, %s, now()
            )
            ON CONFLICT (run_id) DO UPDATE
            SET
                batch_id = EXCLUDED.batch_id,
                stage = EXCLUDED.stage,
                window_key = EXCLUDED.window_key,
                attempt = EXCLUDED.attempt,
                status = EXCLUDED.status,
                exit_code = EXCLUDED.exit_code,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                run_report_ref = EXCLUDED.run_report_ref,
                output_ref = EXCLUDED.output_ref,
                updated_at = now()
            RETURNING run_id::text AS run_id, batch_id, stage, window_key, status
            """,
            (
                report["run_id"],
                report["batch_id"],
                report["stage"],
                window_key,
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
        batch_ids: Optional[List[str]] = None,
    ) -> List[Dict]:
        query = """
            SELECT batch_id, stage, transfer_profile_id, src_lts_ref, dst_staging_ref, priority
            FROM report.batches_needing_input_staging
            WHERE (%s IS NULL OR stage = ANY(%s))
              AND (%s IS NULL OR priority >= %s)
              AND (%s IS NULL OR batch_id = ANY(%s))
            ORDER BY priority DESC, batch_id ASC
            LIMIT %s
        """
        params = (
            stages if stages else None,
            stages if stages else None,
            min_priority,
            min_priority,
            batch_ids if batch_ids else None,
            batch_ids if batch_ids else None,
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
              status = 'requested',
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

    def get_pending_input_transfers_for_polling(self, limit: int = 100) -> List[Dict]:
        """Return requested/active transfers that have a Globus task id."""
        return self.conn.fetch_all(
            """
            SELECT
              transfer_id::text AS transfer_id,
              batch_id,
              stage,
              status,
              globus_task_id,
              poll_attempts,
              requested_at,
              started_at
            FROM logs.transfer_runs
            WHERE direction = 'input_stage'
              AND status IN ('requested', 'active')
              AND globus_task_id IS NOT NULL
            ORDER BY requested_at ASC
            LIMIT %s
            """,
            (limit,),
        )

    def get_input_transfers_for_polling_by_ids(self, transfer_ids: List[str]) -> List[Dict]:
        """Return requested/active input transfers scoped to specific transfer ids."""
        if not transfer_ids:
            return []
        return self.conn.fetch_all(
            """
            SELECT
              transfer_id::text AS transfer_id,
              batch_id,
              stage,
              status,
              globus_task_id,
              poll_attempts,
              requested_at,
              started_at
            FROM logs.transfer_runs
            WHERE direction = 'input_stage'
              AND status IN ('requested', 'active')
              AND globus_task_id IS NOT NULL
              AND transfer_id::text = ANY(%s)
            ORDER BY requested_at ASC
            """,
            (transfer_ids,),
        )

    def update_input_transfer_poll_result(
        self,
        transfer_id: str,
        status: str,
        globus_status_text: Optional[str] = None,
        error_summary: Optional[str] = None,
    ) -> Dict:
        """
        Update transfer status based on Globus polling.

        Expected lifecycle: requested -> active -> completed/failed.
        """
        row = self.conn.fetch_one(
            """
            UPDATE logs.transfer_runs
            SET
              status = %s,
              started_at = CASE WHEN %s = 'active' THEN COALESCE(started_at, now()) ELSE started_at END,
              ended_at = CASE WHEN %s IN ('completed', 'failed') THEN COALESCE(ended_at, now()) ELSE ended_at END,
              globus_status_text = %s,
              error_summary = COALESCE(%s, error_summary),
              last_polled_at = now(),
              poll_attempts = poll_attempts + 1
            WHERE transfer_id = %s::uuid
            RETURNING
              transfer_id::text AS transfer_id,
              status,
              poll_attempts,
              last_polled_at,
              started_at,
              ended_at
            """,
            (status, status, status, globus_status_text, error_summary, transfer_id),
        )
        return row or {}

    def get_raw_files_for_batch_window(
        self,
        batch_id: str,
        start_epoch: int,
        end_epoch: int,
    ) -> List[Dict]:
        """Return LTS RAW files for a batch whose filename epoch falls within [start, end]."""
        return self.conn.fetch_all(
            """
            SELECT file_id, full_path, file_name, fname_ts_epoch, size_bytes
            FROM source.globus_file_index
            WHERE batch_id = %s
              AND namespace = 'LTS'
              AND data_state = 'semifield-upload'
              AND entry_type = 'file'
              AND lower(file_ext) = 'raw'
              AND fname_ts_epoch >= %s
              AND fname_ts_epoch <= %s
            ORDER BY fname_ts_epoch ASC
            """,
            (batch_id, start_epoch, end_epoch),
        )

    def get_transfer_status_for_batch(
        self,
        batch_id: str,
        stage: str,
    ) -> Optional[Dict]:
        """Return the most recent input_stage transfer record for a batch/stage pair."""
        return self.conn.fetch_one(
            """
            SELECT
                transfer_id::text AS transfer_id,
                status,
                globus_task_id,
                ended_at,
                error_summary
            FROM logs.transfer_runs
            WHERE batch_id = %s
              AND stage = %s
              AND direction = 'input_stage'
            ORDER BY requested_at DESC
            LIMIT 1
            """,
            (batch_id, stage),
        )

    def are_inputs_staged_for_batch(self, batch_id: str) -> bool:
        """Return True if RAW files exist on 90daydata for this batch.

        Accepts either a completed logs.transfer_runs record OR direct file
        presence in source.globus_file_index (covers batches staged outside
        the normal transfer workflow, e.g. manual rsync or a prior run).
        """
        row = self.conn.fetch_one(
            """
            SELECT COUNT(*) AS n
            FROM source.globus_file_index
            WHERE batch_id = %s
              AND namespace = '90daydata'
              AND data_state = 'semifield-upload'
              AND entry_type = 'file'
            """,
            (batch_id,),
        )
        return int((row or {}).get("n", 0)) > 0

    def update_lease_slurm_job_id(
        self,
        lease_id: str,
        slurm_job_id: str,
    ) -> Dict:
        """Record the SLURM job ID on the active lease row."""
        row = self.conn.fetch_one(
            """
            UPDATE logs.stage_leases
            SET slurm_job_id = %s, updated_at = now()
            WHERE lease_id = %s::uuid
            RETURNING lease_id::text AS lease_id, slurm_job_id, batch_id, stage
            """,
            (slurm_job_id, lease_id),
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
