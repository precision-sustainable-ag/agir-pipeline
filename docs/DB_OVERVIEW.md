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
TBD
## `report`
TBD
## `registry`
TBD
## `ops`
TBD
