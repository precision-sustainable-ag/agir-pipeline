# AgirDB Complete Table Reference

**Database:** `agir` (PostgreSQL)  
**Purpose:** Track agricultural image processing pipelines from RAW files through developed jpg images, detections, and semifield-cutouts

## Schema overview

### `source` — Physical file inventory

**Purpose:** Authoritative inventory of files discovered across all storage locations (JUNO, NCSU staging, CERES, etc.).
**Typical tables:** `globus_file_index`, `endpoints`, `storage_roots`, `inventory_runs`
**Notes:** Ground truth for “what exists and where,” supporting gap detection and transfer planning.

---

### `processed` — Canonical data products

**Purpose:** Authoritative, structured representation of pipeline outputs (images, detections, segmentations, cutouts, cutout_props), with stable IDs and artifact pointers.
**Typical tables:** `images`, `detections`, `segmentations`, `cutouts`, `cutout_props`, `species`
**Notes:** Durable “usable results” layer; independent of run attempts.

---

### `release` — Curated cutout-level release snapshots (manifest tables)

**Purpose:** Immutable, versioned snapshots of *published* data at the **cutout grain**, designed for trivial export and downstream consumption.
**Typical tables:** `releases`, `cutouts_<release_id>` (manifest snapshot: one row per cutout with file pointers, key props, and provenance keys)
**Notes:** Separates “everything processed” from “officially published,” with a single flat export surface per release.

---

### `logs` — Append-only operational event history

**Purpose:** Immutable history of operational events (stage runs and transfers), with pointers to run bundles (run_report/manifest/logs) and normalized status/error codes.
**Typical tables:** `stage_runs`, `stage_run_items`, `transfer_runs`, `transfer_items`
**Notes:** No mutable “current state” tables required; current/latest status is derived via views.

---

### `report` — Ops-only derived readiness and gap surfaces

**Purpose:** **Derived-only** operational views/materialized views/tables answering readiness, backlog, and gap questions.
**Rule:** **Nothing in `report` is authoritative**; it may be dropped and rebuilt at any time from `source + processed + logs (+ ops/registry)`.
**Typical views/tables:** `latest_stage_status_per_batch`, `batches_ready_for_<stage>`, `missing_on_juno_<data_state>`, `batches_needing_transfer`, `pipeline_backlog_summary`
**Notes:** Powers orchestration queries and operational monitoring only (not public analytics).

---

### `registry` — Versioned dependencies and provenance catalog

**Purpose:** Catalog of models/configs/color matrices/software builds used by stages to enable provenance and reproducibility.
**Typical tables:** `models`, `configs`, `color_matrices`, `stage_definitions`, `software_builds`
**Notes:** Referenced by `logs.stage_runs` (what ran + what it used) and optionally by `processed` records.

---

### `ops` — Control plane: pipeline configuration, enums, and policies

**Purpose:** Small, stable tables that define pipeline semantics and operational policy (what stages exist, allowed statuses/error codes, retry rules, resource profiles, deployment settings).
**Typical tables:** `stages`, `stage_dependencies`, `status_codes`, `error_codes`, `retry_policy`, `stage_limits`, `deployments`, `deployment_settings`
**Notes:** Not high-volume; not derived results. Used by orchestrator, db-api, and reporting definitions to stay consistent.

---

* `source` = what exists physically
* `processed` = what we produced logically
* `logs` = what happened
* `report` = what we want to know (derived)
* `registry` = what produced it
* `release` = what we've curated and published (cutout-level)
* `ops` = how the system is allowed to behave

---

## Schemas Overview

| Schema        | Purpose                                                                                                        | Tables                                                                                                              |
| ------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **source**    | Authoritative inventory of files discovered across all storage locations; answers *what exists and where*.     | `globus_file_index`                                                                                                 |
| **processed** | Canonical, structured representations of derived data products produced by pipeline stages.                    | `images`, `detections`, `segmentations`, `cutouts`, `cutout_props`, `species`                                       |
| **release**   | Immutable, versioned **cutout-level** dataset snapshots designed for simple export and downstream consumption. | `releases`, `cutouts_<release_id>`                                                                                  |
| **logs**      | Append-only operational event history capturing pipeline executions and transfers.                             | `stage_runs`, `stage_run_items`, `transfer_runs`, `transfer_items`                                                  |
| **report**    | **Derived-only, ops-focused** readiness, backlog, and gap surfaces for orchestration and monitoring.           | `latest_stage_status_per_batch`, `batches_ready_for_<stage>`, `missing_on_juno_<state>`, `pipeline_backlog_summary` |
| **registry**  | Versioned dependency and provenance catalog for models, configs, and other pipeline inputs along with pointers.| `models`, `configs`, `color_matrices`, `stage_definitions`, `software_builds`                                       |
| **ops**       | Control-plane configuration defining pipeline semantics, policies, and enums.                                  | `stages`, `stage_dependencies`, `status_codes`, `error_codes`, `retry_policy`, `stage_limits`, `deployments`        |

---

## `source`

### source.globus_file_index

**Purpose:** Complete inventory of all files discovered via Globus across all storage locations (JUNO, CERES, NCSU). This is the source of truth for "what files exist where."

**Use Cases:**
- Discover new batches
- Detect pipeline gaps (RAW exists but no JPG)
- Track batch/file locations across storage systems
- Audit file counts and sizes

#### Column descriptions
| Table column        | Conceptual grouping      | Brief description                                                                    | Examples                                                                        |
| ------------------- | ------------------------ | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| `file_id`           | **Row identity**         | Surrogate primary key for the index row.                                             | • `BIGSERIAL` (auto-increment)<br>• `1234567`                                   |
| `endpoint`          | **Storage location**     | Globus endpoint UUID that was crawled.                                               | • `904c2108-90cf-11e8-9672-0a6d4e044368` (JUNO)                                 |
| `site`              | **Storage location**     | Physical/organizational site label for the storage system.                           | • `JUNO`<br>• `NCSU`<br>• `CERES`                                               |
| `storage_domain`    | **Storage location**     | Logical owner/program tag for the storage tree being indexed.                        | • `screberg`<br>• `dash_agir`<br>• `national_plant_image_repository`            |
| `namespace`         | **Storage location**     | Logical namespace grouping within a domain (often matches a filesystem tier).        | • `longterm_images`<br>• `90daydata`<br>• `project`<br>• `LTS`                  |
| `storage_root`      | **Filesystem**           | Absolute filesystem root path for the indexed tree.                                  | • `/LTS/project/dash_agir`<br>• `/project/dash_agir`                            |
| `rel_path`          | **Filesystem**           | Path relative to `storage_root` for the file or directory.                           | • `semifield-upload/TX_2025-08-18/TX_168753342.RAW`                             |
| `full_path`         | **Filesystem**           | Absolute path to the file or directory (stored explicitly for debugging and audits). | • `/LTS/project/dash_agir/semifield-upload/TX_2025-08-18/raws/img_0001.raw`     |
| `parent_dir`        | **Filesystem structure** | Immediate parent folder name for a *file* (NULL for directories).                    | • `images`<br>• `metadata`<br>• `NULL`                                          |
| `file_name`         | **Filesystem structure** | Base name of the file or directory as returned by `globus ls`.                       | • `TX_168753342.jpg`<br>• `metadata`                                            |
| `entry_type`        | **Filesystem structure** | Indicates whether the indexed item is a file or directory.                           | • `file`<br>• `dir`                                                             |
| `file_ext`          | **File attributes**      | File extension (derived), or NULL for directories.                                   | • `RAW`<br>• `ARW`<br>• `jpg`<br>• `NULL`                                       |
| `size_bytes`        | **File attributes**      | Size in bytes as reported by Globus (files only).                                    | • `1048576` (1 MiB)                                                             |
| `permissions`       | **File attributes**      | POSIX-style numeric filesystem permissions.                                          | • `0644`<br>• `0640`<br>• `0755`                                                |
| `checksum`          | **File integrity**       | File checksum (currently not populated by the indexer).                              | • `NULL` (not implemented)                                                      |
| `batch_id`          | **Batch identity**       | Parsed batch identifier extracted from the path.                                     | • `TX_2025-08-18`<br>• `MD_2025-01-01`                                          |
| `batch_state`       | **Batch identity**       | State/region component derived from `batch_id`.                                      | • `TX`<br>• `MD`<br>• `NC`                                                      |
| `batch_date`        | **Batch identity**       | Date component derived from `batch_id` and cast to DATE.                             | • `2025-08-18`<br>• `NULL` if parse fails                                       |
| `data_state`        | **Batch identity**       | Logical pipeline tree being indexed under `storage_root`.                            | • `semifield-upload`<br>• `semifield-developed-images`<br>• `semifield-cutouts` |
| `mtime_iso`         | **Timestamps**           | File modification time from Globus metadata.                                         | • `2025-12-01 14:22:03+00`                                                      |
| `fname_ts_epoch`    | **Timestamps**           | Timestamp parsed from the filename, as epoch seconds.                                | • `1733412345`<br>• `NULL`                                                      |
| `fname_ts_iso`      | **Timestamps**           | Timestamp parsed from the filename, as TIMESTAMPTZ.                                  | • `2025-08-18 12:34:56+00`<br>• `NULL`                                          |
| `created_at_ts_iso` | **Indexing**             | When the row was inserted into the database.                                         | • Defaults to `now()`                                                           |
| *(unique index)*    | **Indexing**             | Enforces uniqueness on `(endpoint, data_state, storage_root, rel_path)`.             | • `ON CONFLICT DO NOTHING`                                                      |


#### `source.globus_file_index` Table schema
```sql
CREATE TABLE IF NOT EXISTS source.globus_file_index (
    file_id           BIGSERIAL PRIMARY KEY,

    endpoint          TEXT NOT NULL,
    site              TEXT NOT NULL,
    storage_domain    TEXT NOT NULL,
    namespace         TEXT NOT NULL,
    storage_root      TEXT NOT NULL,
    rel_path          TEXT NOT NULL,
    full_path         TEXT NOT NULL,
    parent_dir        TEXT,
    file_name         TEXT NOT NULL,

    entry_type        TEXT NOT NULL,
    file_ext          TEXT,
    size_bytes        BIGINT,
    permissions       TEXT,
    checksum          TEXT,

    batch_id          TEXT,
    batch_state       TEXT,
    batch_date        DATE,

    data_state        TEXT NOT NULL,

    mtime_iso         TIMESTAMPTZ,
    fname_ts_epoch    BIGINT,
    fname_ts_iso      TIMESTAMPTZ,
    created_at_ts_iso TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## `processed`
TBD
## `release`
TBD
## `logs`

### logs.stage_leases

**Purpose:** Mutable lease state for each `(batch_id, stage)` so only one orchestrator owns a stage at a time.

**Use Cases:**
- Claim exclusive stage execution
- Track lease expiry and release outcomes
- Prevent duplicate stage runners

#### Column descriptions
| Table column | Conceptual grouping | Brief description | Examples |
| --- | --- | --- | --- |
| `lease_id` | **Row identity** | UUID primary key for the lease row. | • `gen_random_uuid()` |
| `batch_id` | **Work identity** | Batch currently leased. | • `TX_2025-08-18` |
| `stage` | **Work identity** | Stage currently leased. | • `input_staging` |
| `orchestrator_id` | **Ownership** | Orchestrator instance that claimed the lease. | • `orch-01` |
| `leased_at` | **Timing** | Timestamp when lease became active. | • `now()` |
| `expires_at` | **Timing** | Timestamp when lease expires if not released. | • `2026-04-07 14:10:00+00` |
| `attempt` | **Retry tracking** | Lease claim attempt count (`>= 1`). | • `1`<br>• `2` |
| `state` | **Lease state** | Lease lifecycle state. | • `active`<br>• `released` |
| `released_at` | **Lease state** | Timestamp of release (nullable). | • `NULL` |
| `release_reason` | **Lease state** | Reason for release (nullable). | • `completed` |
| `created_at` | **Audit** | Row creation timestamp. | • `now()` |
| `updated_at` | **Audit** | Last update timestamp. | • `now()` |

#### `logs.stage_leases` Table schema
```sql
CREATE TABLE IF NOT EXISTS logs.stage_leases (
    lease_id         UUID PRIMARY KEY DEFAULT ops.uuid_v4(),
    batch_id         TEXT NOT NULL,
    stage            TEXT NOT NULL,
    orchestrator_id  TEXT NOT NULL,
    leased_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    state            TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'released')),
    released_at      TIMESTAMPTZ NULL,
    release_reason   TEXT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stage_leases_batch_stage_key UNIQUE (batch_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_stage_leases_active_expiry
    ON logs.stage_leases (stage, state, expires_at);

CREATE INDEX IF NOT EXISTS idx_stage_leases_batch_stage
    ON logs.stage_leases (batch_id, stage);
```

### logs.stage_runs

**Purpose:** Append-only execution history for stage runs.

**Use Cases:**
- Record stage execution outcomes
- Query latest status per batch/stage
- Drive reliability and retry reporting

#### Column descriptions
| Table column | Conceptual grouping | Brief description | Examples |
| --- | --- | --- | --- |
| `run_id` | **Row identity** | UUID primary key for a stage run. | • UUID |
| `batch_id` | **Work identity** | Batch processed by the run. | • `TX_2025-08-18` |
| `stage` | **Work identity** | Pipeline stage executed. | • `input_staging` |
| `attempt` | **Retry tracking** | Attempt number (`>= 1`). | • `1` |
| `status` | **Outcome** | Terminal run status. | • `success`<br>• `partial`<br>• `failed` |
| `exit_code` | **Outcome** | Process exit code (nullable). | • `0`<br>• `1` |
| `started_at` | **Timing** | Run start timestamp. | • TIMESTAMPTZ |
| `ended_at` | **Timing** | Run end timestamp (`ended_at >= started_at`). | • TIMESTAMPTZ |
| `run_report_ref` | **Artifacts** | Location/reference for run report bundle. | • URI/path |
| `output_ref` | **Artifacts** | Optional output artifact pointer. | • URI/path |
| `created_at` | **Audit** | Row creation timestamp. | • `now()` |
| `updated_at` | **Audit** | Last update timestamp. | • `now()` |

#### `logs.stage_runs` Table schema
```sql
CREATE TABLE IF NOT EXISTS logs.stage_runs (
    run_id           UUID PRIMARY KEY,
    batch_id         TEXT NOT NULL,
    stage            TEXT NOT NULL,
    attempt          INTEGER NOT NULL CHECK (attempt >= 1),
    status           TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    exit_code        INTEGER NULL,
    started_at       TIMESTAMPTZ NOT NULL,
    ended_at         TIMESTAMPTZ NOT NULL,
    run_report_ref   TEXT NOT NULL,
    output_ref       TEXT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_stage_runs_batch_stage_time
    ON logs.stage_runs (batch_id, stage, ended_at DESC);

CREATE INDEX IF NOT EXISTS idx_stage_runs_stage_status_time
    ON logs.stage_runs (stage, status, ended_at DESC);
```

### logs.transfer_requests

**Purpose:** Requested transfer intents that transfer workers can claim and execute.

**Use Cases:**
- Queue transfer demand by batch/state
- Enable prioritized and lease-safe worker pickup
- Track operator notes and request ownership

#### Column descriptions
| Table column | Conceptual grouping | Brief description | Examples |
| --- | --- | --- | --- |
| `transfer_request_id` | **Row identity** | Bigserial primary key. | • `12345` |
| `batch_id` | **Transfer target** | Batch to transfer. | • `TX_2025-08-18` |
| `data_state` | **Transfer target** | Logical data-state tree to transfer. | • `semifield-upload` |
| `storage_domain` | **Storage scope** | Storage domain for source inventory. | • `dash_agir` |
| `namespace` | **Storage scope** | Namespace/tier within storage domain. | • `longterm_images` |
| `from_site` | **Routing** | Source site code. | • `NCSU` |
| `to_site` | **Routing** | Destination site code. | • `JUNO` |
| `priority` | **Scheduling** | Request priority value. | • `100` |
| `is_enabled` | **Scheduling** | Whether request is eligible for pickup. | • `true` |
| `created_at` | **Audit** | Creation timestamp. | • `now()` |
| `leased_until` | **Leasing** | Soft lease expiration for worker ownership (nullable). | • TIMESTAMPTZ |
| `leased_by` | **Leasing** | Worker currently holding lease (nullable). | • `transfer-worker-1` |
| `notes` | **Metadata** | Optional human note/context. | • ticket text |

#### `logs.transfer_requests` Table schema
```sql
CREATE TABLE IF NOT EXISTS logs.transfer_requests (
  transfer_request_id BIGSERIAL PRIMARY KEY,
  batch_id      TEXT NOT NULL,
  data_state    TEXT NOT NULL,
  storage_domain TEXT NOT NULL,
  namespace     TEXT NOT NULL,
  from_site     TEXT NOT NULL DEFAULT 'NCSU',
  to_site       TEXT NOT NULL DEFAULT 'JUNO',
  priority      INTEGER NOT NULL DEFAULT 100,
  is_enabled    BOOLEAN NOT NULL DEFAULT true,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  leased_until  TIMESTAMPTZ NULL,
  leased_by     TEXT NULL,
  notes         TEXT NULL,
  UNIQUE (batch_id, data_state, storage_domain, namespace, from_site, to_site)
);

CREATE INDEX IF NOT EXISTS idx_transfer_requests_enabled_priority
  ON logs.transfer_requests (is_enabled, priority, created_at);

CREATE INDEX IF NOT EXISTS idx_transfer_requests_lease
  ON logs.transfer_requests (leased_until);
```

### logs.transfer_runs

**Purpose:** Execution history of transfer attempts initiated by orchestration and transfer workers.

**Use Cases:**
- Track transfer lifecycle from request to completion/failure
- Enforce idempotency and active transfer uniqueness
- Provide operational audit trail by batch/stage

#### Column descriptions
| Table column | Conceptual grouping | Brief description | Examples |
| --- | --- | --- | --- |
| `transfer_run_pk` | **Row identity** | Bigserial primary key. | • `9876` |
| `transfer_id` | **External identity** | Stable UUID for a transfer run. | • UUID |
| `direction` | **Transfer type** | Transfer direction enum. | • `input_stage`<br>• `promotion` |
| `batch_id` | **Work identity** | Batch associated with transfer. | • `TX_2025-08-18` |
| `stage` | **Work identity** | Stage that requested transfer. | • `input_staging` |
| `transfer_profile_id` | **Configuration** | Transfer profile used for execution. | • `juno_default` |
| `src_lts_ref` | **Source** | Source LTS reference/path. | • URI/path |
| `dst_staging_ref` | **Destination** | Destination staging reference/path. | • URI/path |
| `priority` | **Scheduling** | Priority value. | • `100` |
| `requested_by` | **Audit** | Request initiator (nullable). | • service/user id |
| `dedupe_key` | **Idempotency** | Optional dedupe key (nullable). | • formatted key |
| `status` | **Lifecycle** | Transfer status enum. | • `requested`<br>• `active`<br>• `completed`<br>• `failed` |
| `requested_at` | **Timing** | Request timestamp. | • `now()` |
| `started_at` | **Timing** | Start timestamp (nullable). | • TIMESTAMPTZ |
| `ended_at` | **Timing** | End timestamp (nullable). | • TIMESTAMPTZ |
| `error_summary` | **Errors** | Error summary text (nullable). | • message |
| `created_at` | **Audit** | Row creation timestamp. | • `now()` |

#### `logs.transfer_runs` Table schema
```sql
CREATE TABLE logs.transfer_runs (
  transfer_run_pk      BIGSERIAL PRIMARY KEY,
  transfer_id          UUID NOT NULL UNIQUE,
  direction            TEXT NOT NULL CHECK (direction IN ('input_stage', 'promotion')),
  batch_id             TEXT NOT NULL,
  stage                TEXT NOT NULL,
  transfer_profile_id  TEXT NOT NULL,
  src_lts_ref          TEXT NOT NULL,
  dst_staging_ref      TEXT NOT NULL,
  priority             INTEGER NOT NULL DEFAULT 100,
  requested_by         TEXT NULL,
  dedupe_key           TEXT NULL,
  status               TEXT NOT NULL CHECK (status IN ('requested','active','completed','failed')),
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at           TIMESTAMPTZ NULL,
  ended_at             TIMESTAMPTZ NULL,
  error_summary        TEXT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_transfer_runs_active_input
  ON logs.transfer_runs (direction, batch_id, stage, dst_staging_ref)
  WHERE status IN ('requested', 'active');

CREATE UNIQUE INDEX idx_transfer_runs_dedupe_key
  ON logs.transfer_runs (dedupe_key)
  WHERE dedupe_key IS NOT NULL;

CREATE INDEX idx_transfer_runs_status_time
  ON logs.transfer_runs (status, requested_at DESC);
```

## `report`
TBD
## `registry`
TBD
## `ops`

### ops.claim_stage_lease

**Purpose:** Atomically claim or renew a stage lease for a `(batch_id, stage)` pair when no active unexpired lease exists.

#### `ops.claim_stage_lease` Function signature
```sql
ops.claim_stage_lease(
  p_batch_id TEXT,
  p_stage TEXT,
  p_orchestrator_id TEXT,
  p_ttl_seconds INTEGER,
  p_attempt INTEGER DEFAULT NULL
) RETURNS TABLE (
  claimed BOOLEAN,
  lease_id UUID,
  batch_id TEXT,
  stage TEXT,
  expires_at TIMESTAMPTZ,
  attempt INTEGER,
  job_workdir_policy JSONB
)
```

### ops.release_stage_lease

**Purpose:** Release an active lease owned by an orchestrator and record release metadata.

#### `ops.release_stage_lease` Function signature
```sql
ops.release_stage_lease(
  p_lease_id UUID,
  p_orchestrator_id TEXT,
  p_release_reason TEXT,
  p_released_at TIMESTAMPTZ DEFAULT NULL
) RETURNS TABLE (
  released BOOLEAN,
  lease_id UUID,
  released_at TIMESTAMPTZ,
  release_reason TEXT
)
```

### agir_db.request_input_transfer

**Purpose:** Idempotently request an `input_stage` transfer, returning existing active/completed transfer when appropriate.

#### `agir_db.request_input_transfer` Function signature
```sql
agir_db.request_input_transfer(
  p_batch_id TEXT,
  p_stage TEXT,
  p_transfer_profile_id TEXT,
  p_src_lts_ref TEXT,
  p_dst_staging_ref TEXT,
  p_requested_by TEXT DEFAULT NULL,
  p_priority INTEGER DEFAULT 100,
  p_dedupe_key TEXT DEFAULT NULL,
  p_request_ts TIMESTAMPTZ DEFAULT NULL
) RETURNS TABLE (
  accepted BOOLEAN,
  transfer_id UUID,
  state TEXT,
  requested_at TIMESTAMPTZ
)
```
