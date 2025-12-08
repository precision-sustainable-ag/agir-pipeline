# Core Reporting Queries (`report_core.sql`)

This file contains a curated set of diagnostic and reporting queries used to evaluate the state of the **AgIR Globus File Index** stored in PostgreSQL.
The queries help track **inventory**, **processing completeness**, **JUNO replication status**, and **batch-level data integrity** across RAWs, developed images, and metadata.

## Output location:

Outputs are saved as csvs in `reports/raw/<YYYY-MM-DD>_<query_name>.csv`

---

## 1. Inventory & High-Level Stats (`cnt`)

### **`cnt_total_stats`**

Provides a global overview of the `source.globus_file_index` table, including:

* Total number of indexed files
* Sum of all file sizes
* First and most recent `created_at_ts_iso` timestamps

Use this query to sanity-check index growth, recency, and ingestion health.

---

### **`cnt_batch_location_state_counts`** 

Aggregates counts of files by `batch_id`, `location`, and `data_state`.
This helps explain *where* batches physically live and *what processing states* are represented (`upload_raw`, `developed_jpg`, `cutouts`, etc.).

---

### **`cnt_batches_raws_count_metadata_count`** 

Reports, per batch:

* Number of RAW files (`upload_raw`)
* Number of JSON metadata files (`developed_jpg` → `metadata/`)

Useful for confirming overall RAW→metadata population coverage across the archive.

---

## 2. JUNO Sync & Replication Diagnostics (`sync`)

These queries identify **files or batches that exist elsewhere but are missing on JUNO**, helping drive copy or recovery operations.

### **`sync_files_missing_images_on_juno`**

Identifies *individual JPG image files* present on other endpoints but **missing from JUNO**, specifically in:

* `data_state = 'developed_jpg'`
* `parent_dir = 'images'`

Use this to generate a transfer list for syncing missing developed JPGs to JUNO.

---

### **`sync_files_missing_metadata_on_juno`**

Same logic as above, but for JSON metadata files under `metadata/`.

This is your go-to query for identifying metadata files that need to be replicated to JUNO.

---

### **`sync_batches_needing_juno_copy_images_and_metadata`** 

Batch-level completeness check for JUNO.
Returns batches that:

* Have RAWs, developed JPGs, and developed JSON metadata *somewhere*,
* But one or more components are *missing on JUNO*.

This provides a high-level view of which batches still require full replication.

---

### **`sync_batches_needing_juno_images_only`** 

Batch-level list of batches that have:

* Developed JPG images somewhere
* But **none on JUNO**

Use this when focusing on image replication only.

---

### **`sync_batches_needing_juno_metadata_only`** 

Same as above but for JSON metadata.
Shows which batches have metadata elsewhere but not on JUNO.

---

## 3. Missing or Incomplete Data by Batch (`miss`)

These queries help diagnose incomplete processing pipelines at the batch level.

### **`miss_batches_missing_upload_raw`** 

Finds batches that have:

* No RAWs in `upload_raw` anywhere
* But DO have downstream products such as JPGs or metadata

This indicates missing or lost RAW sources.

---

### **`miss_batches_raw_missing_metadata_json`** 

For batches that *do* have RAWs, this flags those lacking:

* JSON metadata under `developed_jpg/metadata`

Useful for identifying batches stuck before metadata creation.

---

### **`miss_batches_raw_missing_image_jpg`** 

For batches with RAWs, shows which ones have **no developed JPGs** in `developed_jpg/images`.
These need RAW→JPG development.

---

## 4. Exploration & Utility Queries (`util`)

### **`util_random_samples_10000`** 

Returns 10,000 pseudorandom rows for:

* Quick inspection
* Debugging
* Verifying field distributions

---

### **`util_select_unique_column_values`** 

Builds a flattened list of unique values for key categorical fields:

* `endpoint`
* `location`
* `lts_root`
* `root_path`
* `parent_dir`
* `entry_type`
* `file_ext`
* `data_state`

Useful for schema auditing, understanding data shape, and writing new filters or QA logic.

---

## Running the Queries

All queries can be executed using `psql`:

```bash
psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -f report_core.sql
```

Or run an individual query interactively:

```bash
psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE
```
