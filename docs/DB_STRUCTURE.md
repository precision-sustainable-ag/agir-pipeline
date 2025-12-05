# AgIR Database Blueprint (PostgreSQL)

This document defines the **logical layout** of the AgIR PostgreSQL database used to track:

* raw image files (where they live, what we have)
* cleaned “canonical” image records and metadata
* derived products (bboxes, masks, cutouts, etc.)
* pipeline runs and QC
* public / release-ready datasets

The design uses:

* **One database** (`agir`)
* **Multiple schemas** to separate roles and lifecycle:

  * `core` 
  * `source` (bronze)
  * `canonical` (silver)
  * `release` (gold)
  * `logs`

---

## 1. Database

### `agir` (PostgreSQL database)

Single logical database for all AgIR data:

* SemiField + Field + future domains live together
* Shared reference (species, experiments, etc.)
* Pipeline logging and image status

---

## 2. Schemas and Their Roles

### 2.1 `core` – Shared reference data

Slow-changing, global reference tables used everywhere.

**Examples:**

* `core.species_info`

  * `class_id` (PK)
  * `common_name`
  * `genus`
  * `species`
  * `plant_type`
  * etc.

* `core.marker_locations`

  * `path` (PK)
  * `state`
  * `season`
  * `start_date`, `end_date`


Other candidates: `core.cameras`, `core.bbot_version`, `core.site_settings`.

**Purpose:**
Give all other schemas a consistent vocabulary (species,marker locations, etc.) used throughout the pipeline.

---

### 2.2 `source` – File-level data inventory (`bronze` layer)

Tracks every file discovered on storage (Juno, Ceres, NCSU) via Globus, along with its location, batch, and timestamps.

### Table: `source.globus_file_index`

* `endpoint` – Globus endpoint ID
* `location` – storage site (`JUNO`, `NCSU`, `CERES`)
* `lts_root` – logical LTS root (`longterm_images`, `GROW_DATA`, `dash_agir`, …)
* `root_path` – absolute path to the monitored tree
* `rel_path` – path under `root_path`
* `entry_type` – `file` or `dir`
* `file_ext` – `RAW`, `jpg`, `json`, etc.
* `size_bytes` – file size in bytes
* `batch_id`, `batch_state`, `batch_date` – parsed batch metadata
* `data_state` – `upload_raw`, `developed_jpg`, etc.
* `mtime_epoch` – last-modified time in epoch seconds
* `fname_ts_epoch`, `fname_ts_iso` – optional timestamp parsed from filename
* `created_at_epoch` – when this row was inserted

**Examples:**
* `source.globus_file_index`

```sql
CREATE TABLE source.globus_file_index (
    file_id           BIGSERIAL PRIMARY KEY,

    endpoint          TEXT NOT NULL,  -- Globus endpoint ID
    location          TEXT NOT NULL,  -- 'JUNO', 'NCSU', 'CERES'
    lts_root          TEXT NOT NULL,  -- 'longterm_images', 'longterm_images2', 'GROW_DATA', 'dash_agir'
    root_path         TEXT NOT NULL,  -- e.g. '/LTS/project/dash_agir/semifield-upload'
    rel_path          TEXT NOT NULL,  -- path under root_path, no leading slash
    file_name         TEXT NOT NULL,  -- file name with extension, ('MD_167889349.jpg')

    entry_type        TEXT NOT NULL,  -- 'file' or 'dir'
    file_ext          TEXT,           -- 'RAW','jpg','json', etc (no dot)
    size_bytes        BIGINT,
    checksum          TEXT,           -- optional, can be NULL for now

    batch_id          TEXT,           -- 'MD_2025-01-01'
    batch_state       TEXT,           -- 'MD','TX','NC'
    batch_date        DATE,           -- 2025-01-01

    data_state        TEXT NOT NULL,  -- 'upload_raw','developed_jpg', etc

    mtime_epoch       BIGINT,         -- from Globus last_modified
    fname_ts_epoch    BIGINT,         -- parsed from filename if present
    fname_ts_iso      TIMESTAMPTZ,    -- same as above, ISO
    created_at_ts_iso  TIMESTAMPTZ -- when this row was inserted
);

-- Uniqueness at storage level
CREATE UNIQUE INDEX IF NOT EXISTS ix_source_globus_unique
ON source.globus_file_index(endpoint, data_state, root_path, rel_path);

```
Answer “what files do we have and where are they?” and support detection of missing/duplicate/trash files without deleting history. 

---

## 2.3 `processed` – Cleaned, structured truth (`silver` layer)

Aggregates per-batch, per-`data_state` metrics from `source.globus_file_index`.
Represents **logical images** and derived products in a consistent, relational form.
This is what internal pipelines and analysis primarily work against.

### Table: `processed.batch_index`

* `batch_id`, `data_state` – composite primary key
* `batch_state`, `batch_date` – batch metadata
* `raw_file_count`, `jpg_file_count`, `json_file_count`, `other_file_count`
* `primary_location`, `primary_lts_root` – where the batch lives primarily
* `ingest_status` – `seen`, `indexed`, `failed`, `partial`
* `processing_status` – `raw`, `preprocessed`, `color_corrected`, `detections`, `segmentations`, `species_mapped`, `completed`
* `first_seen_epoch`, `last_seen_epoch`, `last_indexed_epoch`

**Purpose:**
Answers “what do I have for this batch, and what’s its status?”
**Purpose:**
This is the **authoritative internal model**: one row per logical image, plus derived products, all tied back to `core.*` and `source.*`.

**Example** 
* `process.batch_index`

```sql
CREATE TABLE processed.batch_index (
    batch_id              TEXT NOT NULL,    -- 'MD_2025-01-01'
    data_state            TEXT NOT NULL,    -- 'upload_raw','developed_jpg', etc

    -- Derived metadata
    batch_state           TEXT,             -- 'MD','TX','NC' (can be parsed from batch_id)
    batch_date            DATE,             -- 2025-01-01

    -- Inventory counts
    raw_file_count        INTEGER,
    jpg_file_count        INTEGER,
    json_file_count       INTEGER,
    other_file_count      INTEGER,

    raw_file_type         TEXT,             -- optional: 'RAW','rw2', etc, if you care

    -- Where the files physically live
    primary_location      TEXT,             -- 'JUNO','NCSU','CERES'
    primary_lts_root      TEXT,             -- 'longterm_images', 'GROW_DATA', etc

    -- Processing / pipeline status for this (batch_id, data_state)
    ingest_status         TEXT,             -- 'seen','indexed','failed','partial'
    processing_status     TEXT,             -- 'raw','preprocessed','color_corrected','detections','segmentations','species_mapped','completed'

    first_seen_epoch      BIGINT,
    last_seen_epoch       BIGINT,
    last_indexed_epoch    BIGINT,

    PRIMARY KEY (batch_id, data_state)
);

```

So processed is your playground for organizing SemiF/AgIR however you need, without worrying about public contracts yet.

* `processed.image_metadata`
* `processed.detections`
* `processed.segmentations`
* `processed.processing_status`

* `processed.images` – canonical image records
  * `image_id` (PK)
  * `raw_image_id` (FK → `bronze.raw_images`)
  * `experiment_id` (FK → `core.experiments`)
  * `pot_id` (FK → `core.pots`)
  * `capture_datetime`
  * `camera_id` (FK → `core.cameras`)
  * `width_px`, `height_px`
  * `file_format` (`RAW`, `JPG`, etc.)
  * `qc_status` (`ok`, `trash`, `needs_review`)
  * `trash_reason` (nullable text)
  * `is_active` (boolean – whether this image counts as part of the repo)
  * `created_at`, `updated_at`

* `processed.image_files` – multiple representations per image

  * `image_file_id` (PK)
  * `image_id` (FK → `silver.images`)
  * `file_type` (`raw`, `developed_jpg`, `thumbnail`, `debug_overlay`, etc.)
  * `storage_root`
  * `relative_path`
  * `byte_size`, `checksum`
  * `created_at`

* `processed.bbox_instances` – detection/instance metadata

  * `bbox_id` (PK)
  * `image_id` (FK → `silver.images`)
  * `instance_id` (optional “plant instance” per image)
  * `x_min`, `y_min`, `x_max`, `y_max`, `area_px`
  * `species_id` (FK → `core.species`)
  * `source` (`human`, `model_v1`, `model_v2`, …)
  * `confidence` (nullable)
  * `created_at`

* `processed.masks` – mask products

  * `mask_id` (PK)
  * `image_id` (FK → `silver.images`)
  * `bbox_id` (FK → `silver.bbox_instances`, nullable for semantic-only masks)
  * `mask_type` (`instance`, `semantic`, `weed`, `crop`, …)
  * `storage_root`, `relative_path`
  * `format` (`png`, `npz`, `rle`)
  * `created_at`

* `processed.cutouts` – cropped image patches

  * `cutout_id` (PK)
  * `bbox_id` (FK → `silver.bbox_instances`)
  * `image_id` (FK → `silver.images`)
  * `storage_root`, `relative_path`
  * `width_px`, `height_px`
  * `created_at`

* `processed.cutout_properties` - morphological, spectral, and physical properties
  
  * `prop_id` (PK)
  * `cutout_id` (FK → `silver.bbox_instances`)
  * `image_id` (FK → `silver.images`)
  * `rgb_mean`
  * `rgb_std`
  * `is_primary`
  * `touches_border`


---

### 2.4 `gold` – Public / release datasets (release layer)

`gold` defines **named, versioned releases** that are **subsets of `silver`**, packaged for external use.

#### Core tables

* `gold.releases`

  * `release_id` (PK)
  * `name` (e.g. `AgIR-SemiField-v1.0`)
  * `description`
  * `created_at`
  * `created_by`
  * `license`
  * `public_url` (landing page / DOI / bucket URL)
  * `criteria_json` (machine-readable definition: filters, species list, QC rules, etc.)

* `gold.release_images`

  * `release_id` (FK → `gold.releases`)
  * `image_id` (FK → `silver.images`)
  * **PK** (`release_id`, `image_id`)

* `gold.release_bboxes`

  * `release_id` (FK → `gold.releases`)
  * `bbox_id` (FK → `silver.bbox_instances`)
  * **PK** (`release_id`, `bbox_id`)

(Optionally: `gold.release_cutouts`, `gold.release_masks`, etc.)

You can expose release-specific views, e.g.:

* `gold.v_release_image_summary`
* `gold.v_release_annotations`

**Purpose:**
Define **stable, versioned “packages”** of images and annotations that are safe to publish, cite, or share with collaborators.

---

### 2.5 `logs` – Pipelines, QC, and provenance

Tracks **how** data moved through the system, for debugging, audit, and metrics.

**Examples:**

* `logs.pipeline_runs`

  * `run_id` (PK)
  * `pipeline_name` (`raw_scan`, `raw_to_jpg`, `bbox_infer_v1`, etc.)
  * `started_at`, `finished_at`
  * `status` (`success`, `failed`, `partial`)
  * `params_json` (snapshot of config)
  * `node_name` (HPC node)

* `logs.image_pipeline_events`

  * `event_id` (PK)
  * `run_id` (FK → `logs.pipeline_runs`)
  * `image_id` (FK → `silver.images`)
  * `step_name` (`developed_convert`, `qc`, `bbox_infer`, …)
  * `status` (`success`, `failed`, `skipped`)
  * `message` (short text / error)
  * `created_at`

**Purpose:**
Give a time-stamped history of processing for each image and each pipeline run.

---

## 3. Data Flow: Bronze → Silver → Gold

1. **Discover raw files**

   * Scan storage (Juno/Ceres)
   * Insert into `bronze.raw_images` (+ `bronze.upload_batches`)

2. **Build canonical image records**

   * ETL links bronze files to experiments/pots/species metadata
   * Insert into `silver.images`
   * Attach developed JPGs, thumbnails, etc. in `silver.image_files`

3. **Run QC & mark trash**

   * Update `silver.images.qc_status` + `trash_reason`
   * Internal logic decides which images are part of the active repo

4. **Generate derived products**

   * Detection/segmentation → `silver.bbox_instances`, `silver.masks`, `silver.cutouts`

5. **Log pipelines & events**

   * Every run → `logs.pipeline_runs`
   * Per-image events → `logs.image_pipeline_events`

6. **Build releases (gold)**

   * Define criteria for a release (e.g. QC=ok, certain experiments/species)
   * Insert selected `image_id`s into `gold.release_images`
   * Insert corresponding `bbox_id`s into `gold.release_bboxes`
   * Optional: export files + metadata to external storage based on these tables

---

## 4. Naming & Conventions (Guidelines)

* **Schemas**: lifecycle & role

  * `core` – shared reference
  * `bronze` – “as seen on disk”
  * `silver` – cleaned, structured truth
  * `gold` – curated/public releases
  * `logs` – provenance & run history

* **Table names**:

  * Use plural nouns: `images`, `raw_images`, `bboxes`, `masks`.
  * Use suffixes where needed: `_raw`, `_instances`, `_files`, `_events`.

* **Keys**:

  * Use synthetic integer keys (`*_id`) as PKs.
  * Use FKs to connect layers: `silver.images.raw_image_id → bronze.raw_images.raw_image_id`.
  * Use FKs into `core.*` for species, pots, experiments, etc.

---

This README is meant as a **high-level blueprint**.
You can now:

* turn this into `CREATE TABLE` migrations, and
* iterate on fields as you learn more about your pipelines.
