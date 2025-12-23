# AgirDB RAW→JPG Pipeline Requirements
## Engineering Specification v1.0

**Author**: Matthew Kutugata  
**Date**: 2024-12-12  
**System**: Agricultural Image Repository Database (AgirDB)  
**Pipeline**: RAW Image Acquisition → Development → Storage

---

## Executive Summary

Build a production-grade data pipeline to:
1. Track agricultural image files across 3 storage locations (NCSU, JUNO, CERES)
2. Transfer RAW images to central storage (JUNO)
3. Process RAW→JPG on compute nodes (CERES)
4. Return results to central storage
5. Maintain complete audit trail and enable monitoring

**Key Constraint**: Files indexed weekly via Globus, so system must handle eventually-consistent data.

---

## System Context

### Data Architecture
```
Source of Truth: source.globus_file_index (weekly refresh)
    ↓
Processing Tables: processed.batches, processed.transfers, processed.stage_status
    ↓
Reporting Views: report.* (gap detection, completion status)
    ↓
API Layer: Python classes (TransferManager, ProcessingManager)
    ↓
Orchestration: Shell/Python scripts calling Globus CLI + svs-raw-api
```

### Storage Locations
| Site | Type | Purpose | Capacity | Network |
|------|------|---------|----------|---------|
| NCSU | Source | Data acquisition | 100TB+ | 10 Gbps |
| JUNO | Archive | Long-term storage | 500TB+ | 10-200 Gbps |
| CERES | Compute | HPC processing | 500TB+ scratch | 10-200 Gbps |

SciNet compute nodes use 10 Gb/s Ethernet for access and management, 100 Gb/s IP-over-InfiniBand for high-throughput data transfers, and up to 200 Gb/s InfiniBand fabric for MPI and RDMA-based workloads.

### Data States
| State | Location | Description | Typical Size |
|-------|----------|-------------|--------------|
| `semifield-upload` | NCSU/JUNO/CERES | RAW image files (.NEF, .ARW, .CR2) | 25-40 MB/file |
| `semifield-developed-images` | JUNO/CERES | Developed JPG files | 8-12 MB/file |
| `semifield-cutouts` | JUNO | Cropped regions of interest | 100-500 KB/file |

### Source of truth
> Table: `source.globus_file_index` 

| Table column        | Brief description                                                                          | Possible values / examples                                                                                                                              |
| ------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `file_id`           | Surrogate primary key for the index row.                                                   | BIGSERIAL (auto-increment). Example: `1234567`.                                                                                                         |
| `endpoint`          | Globus endpoint UUID that was crawled.                                                     | Example: `904c2108-90cf-11e8-9672-0a6d4e044368` (JUNO).                                                                                                 |
| `site`              | Physical/organizational site label for the storage location.                               | One of: `JUNO`, `NCSU`, `CERES`.                                                                                                                        |
| `storage_domain`    | Logical “owner/program” tag for the storage tree being indexed.                            | One of: `screberg`, `dash_agir`, `national_plant_image_repository`.                                                                                     |
| `namespace`         | Logical namespace grouping within a domain (often matches a filesystem tier/root family).  | Examples from wrapper config: `longterm_images`, `longterm_images2`, `GROW_DATA`, `90daydata`, `project`, `LTS`.                                        |
| `storage_root`      | Absolute filesystem root path for the tree being indexed (base path on that site).         | Examples: `/LTS/project/dash_agir`, `/project/dash_agir`, `/90daydata/national_plant_image_repository`, `/rsstu/users/s/screberg/longterm_images2`.     |
| `rel_path`          | Path relative to `storage_root` for the item (file or directory).                          | Example: `semifield-upload/TX_2025-08-18/TX_168753342.RAW` (shape depends on your tree). Built as `full_path[len(storage_root):].lstrip("/")`.         |
| `full_path`   | Absolute path to the file or directory on the filesystem, constructed by joining `storage_root` and `rel_path`. Stored explicitly for convenience and debugging, even though it is derivable. | Example: `/LTS/project/dash_agir/semifield-upload/TX_2025-08-18/raws/img_0001.raw` |
| `parent_dir`        | Immediate parent folder name for a *file* (NULL for dirs or top-level files).              | Examples: `images`, `metadata`; NULL if `entry_type='dir'` or no parent.                                                                        |
| `file_name`         | Base name of the file/dir as returned by `globus ls`.                                      | Examples: `TX_168753342.jpg`, `TX_2025-08-18`, `metadata`.                                                                                                  |
| `entry_type`        | Indicates whether the indexed item is a file or directory.                                 | `file` or `dir`                                                                                         |
| `file_ext`          | File extension (derived), or NULL for directories.                                         | Examples: `RAW`, `ARW`, `jpg`, `json`; NULL if directory.                                 |
| `size_bytes`        | Size in bytes as reported by Globus (files only).                                          | Example: `1048576` (1 MiB).                                                                                             |
| `permissions`       | POSIX-style numeric filesystem permissions for the item as reported by Globus.             | Examples: `0644`, `0640`, `0755`, `0700` |
| `checksum`          | File checksum (currently not populated by the indexer).                                    | Present column, but inserted as `None` currently                   |
| `batch_id`          | Parsed batch identifier extracted from the full path.                                      | Examples like `TX_2025-08-18`, `MD_2025-01-01`                                                     |
| `batch_state`       | State/region component split from `batch_id`.                                              | Example: `TX`, `MD`, `NC` (depends on `split_batch_id()`).                                                                                              |
| `batch_date`        | Date component split from `batch_id` and cast to a DATE.                                   | Example: `2025-08-18` (stored as DATE). Set to NULL if parse fails.                                                                                     |
| `data_state`        | Logical label for which “tree” is being indexed under `storage_root`.                      | CLI choices: `semifield-upload`, `semifield-developed-images`, `semifield-cutouts` (and used to build `data_dir = Path(storage_root) / data_state`).    |
| `mtime_iso`         | Item modification time from Globus `last_modified`, converted to TIMESTAMPTZ.              | Example: `2025-12-01 14:22:03+00`. Derived from `item.get("last_modified")` → `epoch_to_timestamptz`.                                                   |
| `fname_ts_epoch`    | Timestamp extracted from the filename (if present), as epoch seconds.                      | Example: `1733412345` or NULL if no timestamp in name (depends on `extract_epoch_from_filename`).                                                       |
| `fname_ts_iso`      | Timestamp extracted from the filename (if present), as TIMESTAMPTZ.                        | Example: `2025-08-18 12:34:56+00` or NULL. (Passed as `fname_ts_dt` into `fname_ts_iso` column).                                                        |
| `created_at_ts_iso` | When the row was inserted into the DB (indexing time).                                     | Defaults to `now()` at insert time.                                                                                                                     |
| *(unique index)*    | Enforces one row per `(endpoint, data_state, storage_root, rel_path)` to avoid duplicates. | Conflict-handled as “do nothing” on insert; duplicates won’t be inserted.                                                                               |


### Batch Structure
```
Batch ID format: {STATE}_{YYYY-MM-DD}
Example: MD_2024-06-01

Typical batch:
- 500-2000 RAW files
- 12-80 GB total
- Captured over 4-8 hours
- Contains: /raws/*.RAW, /images/*.jpg, /metadata/*.json, /cutouts/*{.jpg, .png, _mask.png, .json}
```

---

## REQUIREMENT 1: Gap Detection - Missing Files on JUNO

### 1.1 Purpose
Identify RAW image files that do not exist in JUNO but exist in other locations and thus need to be moved to JUNO. (data_state = 'semifield-upload')
Identify developed JPGs files that do not exist in JUNO but exist in other locations and thus need to be moved to JUNO. (data_state = 'semifield-developed-images', parent_dir = 'images')
Identify developed metadata JSON files that do not exist in JUNO but exist in other locations and thus need to be moved to JUNO. (data_state = 'semifield-developed-images', parent_dir = 'metadata')
Identify cutout files (*.png, *.jpg, *_mask.png, *.json) that do not exist in JUNO but exist in other locations and thus need to be moved to JUNO. (data_state = 'semifield-cutouts')

### 1.2 Functional Requirements

**FR-1.1**: System SHALL identify all files where:
- File exists at NCSU or CERES with `data_state = 'semifield-upload'`
- No matching file exists at JUNO with same `batch_id`, `file_name`, and `data_state`

**FR-1.2**: System SHALL provide both file-level and batch-level gap reports

**FR-1.3**: System SHALL report gaps for each `data_state` independently:
- `semifield-upload` (RAW files)
- `semifield-developed-images` (JPG files)  
- `semifield-cutouts` (cutout files)

### 1.3 Data Definitions

**File Match Criteria**:
```sql
Files match when:
  same_batch_id = (source.batch_id = target.batch_id)
  AND same_filename = (source.file_name = target.file_name)
  AND same_state = (source.data_state = target.data_state)
  
Note: Do NOT match on site, storage_domain, namespace, or storage_root (differs between and among sites)
```

**Gap Definition**:
```
Gap exists when:
  ∃ file at source_site
  ∧ ¬∃ matching file at JUNO
  ∧ file.entry_type = 'file' (not directory)
  ∧ file.batch_id IS NOT NULL
```

### 1.4 Acceptance Criteria

**AC-1.1**: Query `report.missing_on_juno` returns files meeting gap criteria  
**AC-1.2**: File count matches manual verification (spot check 3 batches)  
**AC-1.3**: Batch-level aggregation sums correctly (within 1% of file-level count)  
**AC-1.4**: Query executes in <5 seconds for typical dataset (100K files)  
**AC-1.5**: No false positives (files reported missing that actually exist on JUNO)  
**AC-1.6**: No false negatives (files missing from JUNO not reported)  

### 1.5 Edge Cases & Failure Modes

**EC-1.1**: Batch exists at multiple non-JUNO sites (e.g., NCSU + CERES)
- **Behavior**: Use location with largest amount of files
- **Rationale**: Avoid duplicate transfer requests

**EC-1.2**: Batch partially transferred (50% complete, transfer failed)
- **Behavior**: Only missing files show in gap report
- **Rationale**: Self-correcting - re-scan shows what's still missing

**EC-1.3**: File exists on JUNO but in wrong `storage_root`
- **Behavior**: NOT reported as missing (same site = exists). Has never happened before.
- **Rationale**: Storage root is logical organization, not identity

**EC-1.4**: Same batch exists at NCSU and JUNO but different file counts
- **Behavior**: Report files missing from JUNO's version
- **Rationale**: NCSU is source of truth for acquisition

**EC-1.5**: File deleted from JUNO after transfer
- **Behavior**: Shows as missing in next weekly index
- **Rationale**: Eventually consistent with filesystem state

### 1.6 Performance Requirements

**PERF-1.1**: Query response time <5s for 100K files  
**PERF-1.2**: Query response time <30s for 1M files  
**PERF-1.3**: Index size <10% of source table size  

### 1.7 Monitoring & Observability

**MON-1.1**: Track count of batches missing from JUNO over time  
**MON-1.4**: Log query execution time for performance monitoring  


### 1.9 Testing Strategy

**Unit Tests**:
- Use current globus_file_index with known gap scenarios
- Verify query returns expected file IDs
- Test edge cases (empty batch, all files present, duplicates, etc.)

**Integration Tests**:
- Load real data from test environment
- Verify counts match manual filesystem scan
- Test query performance with realistic data volumes

**Validation Tests**:
- Spot check: manually verify 3 batches reported as missing
- Negative test: verify JUNO files not reported as missing

---

## REQUIREMENT 2: Data Transfer - NCSU → JUNO

### 2.1 Purpose
Transfer RAW image files from source location (NCSU) to central archive (JUNO) using Globus.

### 2.2 Functional Requirements

**FR-2.1**: System SHALL track transfer lifecycle: pending → in_progress → completed/failed

**FR-2.2**: System SHALL store Globus task ID for external monitoring

**FR-2.3**: System SHALL support batch-level transfers (not individual files)

**FR-2.4**: System SHALL update progress during transfer (bytes/files completed)

**FR-2.5**: System SHALL record transfer timing (requested, started, completed)

**FR-2.6**: System SHALL support retry of failed transfers

**FR-2.7**: System SHALL prevent duplicate transfers (check if already in progress)

### 2.3 Data Definitions

**Transfer States**:
```
pending       - Requested but not started
in_progress   - Globus task active
completed     - Successfully finished
failed        - Globus task failed
cancelled     - Manually cancelled
```

**Transfer Record**:
```sql
{
  transfer_id: int (PK),
  batch_id: str,
  source_site: str,
  destination_site: str,
  status: enum,
  globus_task_id: str (UUID),
  file_count: int,
  bytes_total: bigint,
  bytes_transferred: bigint,
  transfer_rate_mbps: numeric,
  retry_count: int,
  error_message: text,
  requested_at: timestamptz,
  started_at: timestamptz,
  completed_at: timestamptz
}
```

### 2.4 Acceptance Criteria

**AC-2.1**: Can create transfer request for batch  
**AC-2.2**: Transfer request prevents duplicate (returns existing if in-progress)  
**AC-2.3**: Can associate Globus task ID after submission  
**AC-2.4**: Progress updates every 60 seconds during transfer  
**AC-2.5**: Completion sets status, calculates duration  
**AC-2.6**: Failed transfer stores error message  
**AC-2.7**: Retry increments retry_count, creates new Globus task  

### 2.5 Edge Cases & Failure Modes

**EC-2.1**: Transfer request while previous transfer in progress
- **Behavior**: Return existing transfer_id, log warning
- **Rationale**: Prevent duplicate transfers

**EC-2.2**: Globus task submitted but system crashes before storing task_id
- **Behavior**: Orphaned Globus task (no DB record)
- **Solution**: Manual cleanup, log correlation by timestamp

**EC-2.3**: Globus task completes but DB update fails
- **Behavior**: Status stuck in 'in_progress', but files transferred
- **Solution**: Next gap detection shows files present, manual status update

**EC-2.4**: Partial transfer (network failure after 80% complete)
- **Behavior**: Status='failed', but some files transferred
- **Solution**: Retry transfers only missing files (gap detection self-corrects)

**EC-2.5**: Source files deleted during transfer
- **Behavior**: Globus task fails with "source not found"
- **Solution**: Mark failed, alert for investigation

**EC-2.6**: Destination out of space
- **Behavior**: Globus task fails with "quota exceeded"
- **Solution**: Alert admin, pause transfers until space available

**EC-2.7**: Transfer takes >24 hours (very large batch)
- **Behavior**: Continue monitoring, no timeout
- **Rationale**: Some batches legitimately large

### 2.6 Performance Requirements

**PERF-2.1**: Transfer throughput >500 MB/s (Globus capability)  
**PERF-2.2**: Support concurrent transfers (5+ batches simultaneously)  
**PERF-2.3**: Progress polling overhead <1% of transfer time  
**PERF-2.4**: DB operations <100ms per update  

### 2.7 Monitoring & Observability

**MON-2.1**: Track active transfer count  
**MON-2.2**: Track average transfer rate (MB/s) by route  
**MON-2.3**: Alert if transfer fails 3+ times  
**MON-2.4**: Alert if transfer stuck in 'in_progress' >48 hours  
**MON-2.5**: Dashboard showing: active transfers, queued transfers, failed transfers  
**MON-2.6**: Daily report: batches transferred, total GB, avg duration  

### 2.8 API Interface

```python
# Request transfer
transfer_id = db.transfers.request_transfer(
    batch_id: str,
    source_site: str,
    dest_site: str,
    file_count: Optional[int] = None,  # Auto-count if None
    bytes_total: Optional[int] = None   # Auto-sum if None
) -> int

# Start transfer (after Globus submission)
db.transfers.start_transfer(
    transfer_id: int,
    globus_task_id: str
) -> None

# Update progress (polling loop)
db.transfers.update_progress(
    transfer_id: int,
    files_transferred: int,
    bytes_transferred: int,
    transfer_rate_mbps: float
) -> None

# Complete transfer
db.transfers.complete_transfer(
    transfer_id: int,
    success: bool,
    error_message: Optional[str] = None
) -> None

# Query methods
db.transfers.get_active_transfers() -> List[Dict]
db.transfers.get_pending_transfers() -> List[Dict]
db.transfers.get_failed_transfers() -> List[Dict]
db.transfers.get_transfer_by_id(transfer_id: int) -> Dict
```

### 2.9 Testing Strategy

**Unit Tests**:
- Test state transitions (pending→in_progress→completed)
- Test duplicate prevention
- Test retry logic
- Test error handling

**Integration Tests**:
- Submit real Globus transfer (small test batch)
- Verify status updates
- Test progress polling
- Test completion handling

**Load Tests**:
- Submit 10 concurrent transfers
- Verify no race conditions
- Verify DB performance remains acceptable

**Failure Tests**:
- Simulate Globus failure (mock API)
- Simulate network failure (disconnect during transfer)
- Simulate DB failure (during update)
- Verify system recovers gracefully

---

## REQUIREMENT 3: Gap Detection - Files Needing RAW→JPG Processing

### 3.1 Purpose
Identify RAW files that do not have corresponding JPG files at ANY location (not just JUNO).

### 3.2 Functional Requirements

**FR-3.1**: System SHALL check for JPG existence at ALL sites before flagging for processing

**FR-3.2**: System SHALL match RAW to JPG by base filename (strip extension)

**FR-3.3**: System SHALL handle multiple RAW formats (.NEF, .ARW, .CR2, .DNG)

**FR-3.4**: System SHALL calculate batch completion percentage

**FR-3.5**: System SHALL distinguish between:
- Not started (0% complete)
- Partial (1-99% complete)
- Complete (100%)

**FR-3.6**: System SHALL support site-specific queries (e.g., "files at JUNO needing processing")

### 3.3 Data Definitions

**File Matching Logic**:
```sql
RAW file: MD_1234567890.NEF
JPG file: MD_1234567890.jpg

Match = REGEXP_REPLACE(raw.file_name, '\.(NEF|ARW|CR2|DNG)$', '', 'i')
      = REGEXP_REPLACE(jpg.file_name, '\.(jpg|jpeg)$', '', 'i')

Example matches:
  MD_1234.NEF    ↔ MD_1234.jpg     ✓
  MD_1234.NEF    ↔ MD_1234.JPG     ✓
  MD_1234.arw    ↔ MD_1234.jpeg    ✓
  MD_1234.NEF    ↔ MD_9999.jpg     ✗
```

**Processing Status**:
```
not_started  - No JPGs exist anywhere (0% complete)
partial      - Some JPGs exist (1-99% complete)
complete     - All RAW have JPG somewhere (100% complete)
```

**Completion Percentage**:
```
percent_complete = (files_with_jpg / total_raw_files) * 100

Example:
  Batch: 100 RAW files
  JPGs exist: 80 (60 at JUNO, 20 at CERES)
  Files needing processing: 20
  Percent complete: 80%
  Status: partial
```

### 3.4 Acceptance Criteria

**AC-3.1**: RAW file with JPG at CERES (not JUNO) does NOT show as needing processing  
**AC-3.2**: RAW file with JPG at both JUNO and CERES does NOT show as needing processing  
**AC-3.3**: RAW file with NO JPG anywhere DOES show as needing processing  
**AC-3.4**: Batch completion percentage accurate (within 1%)  
**AC-3.5**: Query executes in <5 seconds for typical batch  
**AC-3.6**: Site filter works correctly (only RAW at specified site)  

### 3.5 Edge Cases & Failure Modes

**EC-3.1**: JPG exists but corrupted (0 bytes)
- **Behavior**: Still counts as "exists" (not re-processed)
- **Solution**: Separate validation step to detect corruption

**EC-3.2**: JPG exists with different naming convention
- **Behavior**: Not matched, RAW reported as needing processing
- **Solution**: Standardize naming in processing pipeline

**EC-3.3**: RAW processed locally but JPG not uploaded to any Globus site
- **Behavior**: RAW reported as needing processing
- **Rationale**: If not indexed, doesn't exist from system perspective

**EC-3.4**: Batch has mix of processed and unprocessed files at same site
- **Behavior**: Correctly reports partial completion
- **Use case**: Batch processing interrupted, resumed later

**EC-3.5**: Multiple JPGs exist for same RAW (different processing settings)
- **Behavior**: Any JPG match = considered processed
- **Rationale**: At least one conversion exists

**EC-3.6**: RAW file extension in uppercase (.NEF) vs JPG in lowercase (.jpg)
- **Behavior**: Case-insensitive matching (handled by REGEXP_REPLACE)
- **Solution**: Use 'i' flag in regex

### 3.6 Performance Requirements

**PERF-3.1**: Gap detection query <5s for single batch  
**PERF-3.2**: Gap detection query <30s for all batches  
**PERF-3.3**: Completion percentage calculation <2s per batch  

### 3.7 Monitoring & Observability

**MON-3.1**: Track count of batches by status (not_started, partial, complete)  
**MON-3.2**: Track total files needing processing  
**MON-3.3**: Track oldest partial batch (stalled processing indicator)  
**MON-3.4**: Dashboard showing: processing pipeline funnel (raw→jpg→metadata→cutouts)  
**MON-3.5**: Alert if batch stuck in 'partial' >7 days  

### 3.8 API Interface

```python
# File-level gap detection
files = db.processing.get_files_needing_processing(
    batch_id: str,
    site: Optional[str] = None,  # Filter RAW files at this site
    limit: Optional[int] = None
) -> List[Dict]

# Returns: [{file_id, batch_id, site, rel_path, file_name, 
#            size_bytes, file_ext}, ...]

# Batch-level status
batches = db.processing.get_batches_needing_processing(
    site: Optional[str] = None,
    min_completion: float = 0.0,  # Filter by % complete
    max_completion: float = 99.9,
    limit: Optional[int] = None
) -> List[Dict]

# Returns: [{batch_id, site, raw_count, jpg_count, 
#            files_needing_processing, percent_complete, 
#            processing_status}, ...]

# Detailed batch status
status = db.processing.get_batch_processing_status(
    batch_id: str
) -> Dict

# Returns: {batch_id, raw_count, jpg_count_all_sites, 
#           jpg_count_on_juno, files_needing_processing,
#           files_already_processed, percent_complete,
#           processing_status}
```

### 3.9 Testing Strategy

**Unit Tests**:
- Test filename matching (various extensions)
- Test case insensitivity
- Test completion percentage calculation
- Test status classification

**Integration Tests**:
- Create test batch with known RAW/JPG distribution
- Verify gap detection finds correct files
- Verify completion percentage accurate
- Test site filtering

**Validation Tests**:
- Manual verification: spot check 3 batches
- Cross-check: compare with filesystem scan
- Edge case testing: partial batches, mixed formats

**Scenario Tests**:
```
Scenario 1: Batch never processed
  Setup: 100 RAW at NCSU, 0 JPG anywhere
  Expected: 100 files need processing, 0% complete, status=not_started

Scenario 2: Batch fully processed at CERES (not transferred back)
  Setup: 100 RAW at JUNO, 100 JPG at CERES, 0 JPG at JUNO
  Expected: 0 files need processing, 100% complete, status=complete

Scenario 3: Batch partially processed
  Setup: 100 RAW at JUNO, 60 JPG at JUNO, 20 JPG at CERES, 20 no JPG
  Expected: 20 files need processing, 80% complete, status=partial

Scenario 4: Duplicate processing
  Setup: 100 RAW at JUNO, 100 JPG at JUNO, 100 JPG at CERES
  Expected: 0 files need processing, 100% complete, status=complete
```

---

## REQUIREMENT 4-6: Data Transfers (JUNO ↔ CERES)

**Note**: Requirements 4 (JUNO→CERES) and 6 (CERES→JUNO) use same transfer mechanism as Requirement 2.

### 4.1 Additional Considerations

**Storage Allocation**:
- CERES scratch space limited (50TB)
- Must clean up after processing
- Transfer only batches actively being processed

**Processing Location Selection**:
- Prefer CERES for large batches (>1000 files)
- Can use JUNO for small batches (<100 files) if CERES busy
- Load balance across CERES nodes if multiple available

**Data Lifecycle**:
```
1. JUNO → CERES (transfer RAW files)
2. Process at CERES (RAW → JPG)
3. CERES → JUNO (transfer JPG files)
4. Cleanup CERES scratch (delete RAW and JPG)
```

### 4.2 API Extensions

```python
# Get optimal processing location
location = db.processing.get_optimal_processing_site(
    batch_id: str,
    file_count: int,
    total_bytes: int
) -> str  # Returns 'CERES', 'CERES2', 'JUNO'

# Schedule cleanup after processing
db.processing.schedule_cleanup(
    batch_id: str,
    site: str,
    delay_hours: int = 24  # Keep for 24h after completion
) -> None
```

---

## REQUIREMENT 5: RAW→JPG Processing with svs-raw-api

### 5.1 Purpose
Convert RAW camera files to JPG format using the svs-raw-api library.

### 5.2 Functional Requirements

**FR-5.1**: System SHALL track job at batch level (not individual files initially)

**FR-5.2**: System SHALL log file-level processing events

**FR-5.3**: System SHALL support partial batch processing (resume after failure)

**FR-5.4**: System SHALL track processing metrics (duration, success rate)

**FR-5.5**: System SHALL handle processing failures gracefully (no crash on bad file)

**FR-5.6**: System SHALL support parallel processing (multiple files simultaneously)

**FR-5.7**: System SHALL validate output files (size, format)

### 5.3 Data Definitions

**Job States**:
```
pending      - Created but not started
in_progress  - Currently processing
completed    - Finished successfully
failed       - Processing failed
cancelled    - Manually cancelled
```

**Job Record**:
```sql
{
  batch_id: str (PK component),
  stage: str (PK component) = 'raw_to_jpg',
  status: enum,
  job_id: str,
  hostname: str,
  started_at: timestamptz,
  completed_at: timestamptz,
  duration_seconds: numeric,
  success: boolean,
  files_processed: int,
  files_failed: int,
  error_message: text,
  metadata: jsonb  # Processing parameters
}
```

**Processing Event**:
```sql
{
  event_id: bigint,
  event_type: enum,  # 'file.started', 'file.completed', 'file.failed'
  batch_id: str,
  stage: str = 'raw_to_jpg',
  message: text,
  metadata: jsonb  # {input_path, output_path, duration_sec, file_size, ...}
}
```

### 5.4 Acceptance Criteria

**AC-5.1**: Can start processing job for batch  
**AC-5.2**: Each file logs start and end event  
**AC-5.3**: Failed files logged with error details  
**AC-5.4**: Job completion updates counts (files_processed, files_failed)  
**AC-5.5**: Partial batch can be resumed (only processes remaining files)  
**AC-5.6**: Output files validated (exist, size >0, format correct)  
**AC-5.7**: Processing rate >10 files/minute on typical hardware  

### 5.5 Edge Cases & Failure Modes

**EC-5.1**: Corrupted RAW file (cannot be parsed)
- **Behavior**: Log as failed, continue with remaining files
- **Event**: {event_type: 'file.failed', error: 'Corrupted file'}

**EC-5.2**: Out of disk space during processing
- **Behavior**: Fail job, log error, halt processing
- **Recovery**: Clear space, resume from last successful file

**EC-5.3**: Process killed (SIGKILL, node failure)
- **Behavior**: Job stuck in 'in_progress'
- **Recovery**: Manual status update, restart job

**EC-5.4**: svs-raw-api crashes on specific file
- **Behavior**: Log as failed, continue processing
- **Investigation**: Save problematic file path for debugging

**EC-5.5**: Output file generated but has issues (0 bytes, wrong format)
- **Behavior**: Validation fails, log as failed
- **Action**: Re-process file

**EC-5.6**: Multiple jobs try to process same batch
- **Behavior**: Second job fails to start (status already 'in_progress')
- **Solution**: Check status before starting

**EC-5.7**: Processing takes >24 hours (very large batch)
- **Behavior**: Continue processing, no timeout
- **Monitoring**: Alert if exceeds expected duration by 2x

### 5.6 Performance Requirements

**PERF-5.1**: Single file processing <10 seconds (typical 25MB RAW)  
**PERF-5.2**: Batch throughput >10 files/minute (sequential)  
**PERF-5.3**: Support 4-8 parallel workers per node  
**PERF-5.4**: Memory usage <4GB per worker  
**PERF-5.5**: No memory leaks (constant memory over long runs)  

### 5.7 Quality Requirements

**QUAL-5.1**: Output JPG dimensions match RAW (within rotation)  
**QUAL-5.2**: Output JPG EXIF preserved from RAW  
**QUAL-5.3**: Output JPG quality >90% (minimize compression artifacts)  
**QUAL-5.4**: Color accuracy verified (spot check against commercial converters)  

### 5.8 Monitoring & Observability

**MON-5.1**: Track active processing jobs  
**MON-5.2**: Track average processing time per file  
**MON-5.3**: Track success rate (files_processed / total_files)  
**MON-5.4**: Alert if success rate <95%  
**MON-5.5**: Alert if job stuck in 'in_progress' >6 hours  
**MON-5.6**: Dashboard showing: processing queue, throughput, error rate  
**MON-5.7**: Track most common failure reasons (top 5)  

### 5.9 API Interface

```python
# Start processing job
job_id = db.processing.start_job(
    batch_id: str,
    site: str,
    stage: str = 'raw_to_jpg',
    job_id: Optional[str] = None,  # Auto-generate if None
    metadata: Optional[Dict] = None  # Processing parameters
) -> str

# Log file processing (called by worker)
db.processing.log_file_event(
    batch_id: str,
    stage: str,
    event_type: str,  # 'file.started', 'file.completed', 'file.failed'
    file_name: str,
    metadata: Optional[Dict] = None  # {input_path, output_path, duration, error}
) -> None

# Update job progress (periodic updates)
db.processing.update_job_progress(
    batch_id: str,
    stage: str,
    files_processed: int,
    files_failed: int
) -> None

# Complete job
db.processing.complete_job(
    batch_id: str,
    stage: str,
    success: bool,
    error_message: Optional[str] = None
) -> None

# Query methods
db.processing.get_job_progress(batch_id: str, stage: str) -> Dict
db.processing.get_active_jobs() -> List[Dict]
db.processing.get_failed_jobs() -> List[Dict]
```

### 5.10 Testing Strategy

**Unit Tests**:
- Test job lifecycle (pending→in_progress→completed)
- Test progress updates
- Test error handling
- Test file event logging

**Integration Tests**:
- Process small test batch (10 files)
- Verify output files created
- Verify event logging
- Test failure handling (bad file in batch)

**Performance Tests**:
- Process 100 file batch, measure throughput
- Test parallel processing (4 workers)
- Monitor memory usage over 1000 files

**Validation Tests**:
- Visual inspection of output JPGs
- EXIF data comparison (input vs output)
- Dimension verification
- Color accuracy spot checks

**Failure Tests**:
- Corrupt file in batch
- Out of space during processing
- Kill process mid-batch
- Bad input path
- Verify system recovers gracefully

---

## REQUIREMENT 7: Logging & Audit Trail

### 7.1 Purpose
Maintain comprehensive audit trail of all pipeline operations for debugging, compliance, and monitoring.

### 7.2 Functional Requirements

**FR-7.1**: System SHALL log every transfer operation (request, start, progress, complete)

**FR-7.2**: System SHALL log every processing operation (start, file results, complete)

**FR-7.3**: System SHALL log with severity levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)

**FR-7.4**: System SHALL include contextual metadata (batch_id, job_id, stage)

**FR-7.5**: System SHALL support structured logging (JSONB metadata field)

**FR-7.6**: System SHALL retain logs for minimum 90 days

**FR-7.7**: System SHALL support log queries by time, batch, severity, event type

### 7.3 Data Definitions

**Event Types**:
```
transfer.requested   - Transfer created
transfer.started     - Globus task submitted
transfer.progress    - Progress update
transfer.completed   - Transfer successful
transfer.failed      - Transfer failed

job.started          - Processing job started
file.started         - File processing started
file.completed       - File processing successful
file.failed          - File processing failed
job.completed        - Processing job completed

system.error         - System-level error
validation.warning   - Data validation warning
```

**Severity Levels**:
```
DEBUG    - Detailed diagnostic information
INFO     - General informational events
WARNING  - Warning conditions (non-critical)
ERROR    - Error conditions (operation failed)
CRITICAL - Critical conditions (system failure)
```

### 7.4 Acceptance Criteria

**AC-7.1**: All transfer operations logged  
**AC-7.2**: All processing operations logged  
**AC-7.3**: Errors include stack traces (when applicable)  
**AC-7.4**: Can query logs by date range  
**AC-7.5**: Can query logs by batch_id  
**AC-7.6**: Can filter by severity  
**AC-7.7**: Log query response time <2 seconds  

### 7.5 Edge Cases & Failure Modes

**EC-7.1**: Log database unavailable during operation
- **Behavior**: Queue logs in memory, flush when available
- **Fallback**: Write to local file if queue full

**EC-7.2**: Log table grows too large (>100M rows)
- **Behavior**: Partition by month, archive old partitions
- **Policy**: Keep last 3 months online, archive older

**EC-7.3**: Sensitive data in logs (credentials, PII)
- **Behavior**: Sanitize before logging
- **Review**: Regular audit of log content

**EC-7.4**: Log query takes too long (large time range)
- **Behavior**: Limit results, suggest narrower query
- **Solution**: Time-based indexes

### 7.6 Performance Requirements

**PERF-7.1**: Log write <50ms (async, non-blocking)  
**PERF-7.2**: Support 100+ events/second  
**PERF-7.3**: Log query <2s for typical filters  
**PERF-7.4**: Table size <10GB per month  

### 7.7 Monitoring & Observability

**MON-7.1**: Track log event rate  
**MON-7.2**: Alert on ERROR spike (>10 errors/minute)  
**MON-7.3**: Alert on CRITICAL events (immediate)  
**MON-7.4**: Dashboard showing: error trends, top error types, event volume  
**MON-7.5**: Weekly digest: error summary, warnings, system health  

### 7.8 API Interface

```python
# Log event (internal use)
db.events.log_event(
    event_type: str,
    severity: str,
    message: str,
    batch_id: Optional[str] = None,
    stage: Optional[str] = None,
    job_id: Optional[str] = None,
    metadata: Optional[Dict] = None,
    error_type: Optional[str] = None,
    stack_trace: Optional[str] = None
) -> None

# Query logs
events = db.events.get_events(
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    batch_id: Optional[str] = None,
    severity: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 1000
) -> List[Dict]

# Get recent errors
errors = db.events.get_recent_errors(hours: int = 24) -> List[Dict]

# Get batch event timeline
timeline = db.events.get_batch_timeline(batch_id: str) -> List[Dict]
```

### 7.9 Testing Strategy

**Unit Tests**:
- Test event creation
- Test severity filtering
- Test metadata serialization

**Integration Tests**:
- Log 1000 events, verify all stored
- Query with various filters
- Test concurrent logging

**Performance Tests**:
- Log 10,000 events, measure time
- Query over 1 month of data
- Test index efficiency

---

## REQUIREMENT 8: Pipeline Completion Status

### 8.1 Purpose
Provide master view of pipeline status showing which batches are fully processed and available on JUNO.

### 8.2 Functional Requirements

**FR-8.1**: System SHALL track batches through complete pipeline:
- RAW files acquired
- RAW→JPG converted
- JPG files on JUNO
- (Future: Metadata extracted, Cutouts generated)

**FR-8.2**: System SHALL classify batches into statuses:
- `complete` - All RAW have JPG on JUNO
- `needs_transfer` - All RAW processed but JPGs not on JUNO
- `partial_processing` - Some files processed, some not
- `not_started` - No processing yet

**FR-8.3**: System SHALL provide timestamps for key milestones

**FR-8.4**: System SHALL support filtering by status, date range, batch_state

### 8.3 Acceptance Criteria

**AC-8.1**: Complete batches show in completion report  
**AC-8.2**: Status classification accurate (manual verification)  
**AC-8.3**: Timestamps match actual operation times (±1 second)  
**AC-8.4**: Can generate daily completion report  
**AC-8.5**: Can generate monthly summary statistics  

### 8.4 API Interface

```python
# Get completed batches
batches = db.analytics.get_completed_batches(
    after_date: Optional[date] = None,
    batch_state: Optional[str] = None,
    limit: Optional[int] = None
) -> List[Dict]

# Get pipeline summary
summary = db.analytics.get_pipeline_summary() -> Dict
# Returns: {total_batches, complete, partial, not_started, 
#           total_files, total_gb}

# Get batch completion detail
detail = db.analytics.get_batch_completion_detail(
    batch_id: str
) -> Dict
# Returns: {batch_id, status, raw_count, jpg_count, 
#           transfer_count, processing_completed_at, 
#           transfer_completed_at}
```

---

## Cross-Cutting Concerns

### Idempotency
All operations SHALL be idempotent where possible:
- Transfer request: Returns existing if in-progress
- Processing job: Returns existing if in-progress
- Gap detection: Always reflects current filesystem state

### Concurrency
System SHALL handle concurrent operations:
- Multiple users querying simultaneously
- Multiple workers processing different batches
- Transfer and processing operations on same batch (serialize)

### Data Consistency
System SHALL maintain consistency:
- DB transactions for related updates
- Atomic status transitions
- Eventual consistency with filesystem (weekly refresh)

### Security
System SHALL implement security:
- No credentials in logs
- Database access via .pgpass
- Globus authentication via CLI
- Audit trail for all operations

### Scalability
System SHALL scale to:
- 10,000+ batches
- 10,000,000+ files
- 1 PB+ total storage
- 100+ concurrent operations

### Maintainability
System SHALL be maintainable:
- Clear component boundaries
- Comprehensive logging
- Self-documenting code
- Automated tests (unit, integration, end-to-end)

---

## Questions for Clarification

### 1. Processing Configuration
**Q**: What processing parameters for svs-raw-api?
- JPG quality level?
- Color space (sRGB vs Adobe RGB)?
- Resize/downsample?
- Embedded thumbnail?

**Default**: Use svs-raw-api defaults unless specified

### 2. Error Handling Policy
**Q**: How many retries before giving up?
- Transfer failures: 3 retries
- Processing failures: 1 retry (file-specific issues unlikely to resolve)
- Network failures: 5 retries with exponential backoff

**Decision needed**: Finalize retry policies

### 3. Cleanup Policy
**Q**: When to delete files from CERES scratch?
- Immediately after transfer back to JUNO?
- Keep for N days for verification?
- Manual cleanup only?

**Recommendation**: Keep 24 hours, then auto-cleanup

### 4. Monitoring Alerting
**Q**: Who gets alerts? How?
- Email to team?
- Slack channel?
- PagerDuty for critical?

**Decision needed**: Set up notification channels

### 5. Batch Priority
**Q**: Process batches FIFO or by priority?
- Oldest first?
- By research project priority?
- By batch size (small first)?

**Recommendation**: Oldest first (FIFO)

### 6. Data Validation
**Q**: Validate files after transfer?
- Checksum verification?
- File size comparison?
- Format validation?

**Recommendation**: At minimum, verify file size matches

### 7. Historical Data
**Q**: Migrate data from old SQLite databases?
- Yes, use existing Migration component
- No, start fresh

**Decision needed**: Confirm migration strategy

---

## Success Metrics

### Operational Metrics
- **Throughput**: >500 GB/day transferred
- **Latency**: Batch processing <24 hours from acquisition to JUNO
- **Reliability**: >99% success rate
- **Availability**: System uptime >99.5%

### Quality Metrics
- **Completeness**: 100% of acquired RAW files have JPG
- **Accuracy**: <0.1% false positives in gap detection
- **Consistency**: DB state matches filesystem within 1 week

### Business Metrics
- **Storage efficiency**: JPG size 30-40% of RAW size
- **Cost**: Processing cost <$0.01 per file
- **Time to insights**: Research data available <48 hours after acquisition

---

## Appendix A: Data Flow Diagram

```
[NCSU Source]
     ↓ (weekly index)
[globus_file_index] ← Source of Truth
     ↓
[Gap Detection Views]
     ↓
[TransferManager API] → [Globus CLI] → [JUNO Archive]
     ↓                                       ↓
[ProcessingManager API] ← [Queue]          ↓
     ↓                                       ↓
[Transfer to CERES] ←─────────────────────┘
     ↓
[RAW→JPG Processing] (svs-raw-api)
     ↓
[Transfer to JUNO] → [JUNO Archive]
     ↓
[Completion Report]
```

## Appendix B: Database Schema Summary

```
source/
  globus_file_index (10M+ rows)
    - Weekly refreshed via globus_index.py
    - Unique: (endpoint, data_state, storage_root, rel_path)

processed/
  batches (10K+ rows)
    - Synced from globus_file_index via InventorySync
    - Tracks file counts, completion flags
  
  transfers (100K+ rows)
    - Tracks Globus transfers
    - Status: pending/in_progress/completed/failed
  
  stage_status (50K+ rows)
    - Tracks processing jobs
    - Status: in_progress/completed/failed
  
  events (1M+ rows)
    - Audit trail
    - Partitioned by month
  
  images (10M+ rows)
    - Image metadata (EXIF, detections)
    - Future: populated after JPG processing

report/
  missing_on_juno (view)
  files_needing_raw_to_jpg (view)
  batch_processing_status (view)
  pipeline_complete (view)
  + 10 more analytics views
```

## Appendix C: Technology Stack

**Database**: PostgreSQL 14+
- JSONB for flexible metadata
- Partitioning for large tables
- GIN indexes for JSONB queries

**Data Transfer**: Globus CLI
- Batch transfers
- Progress monitoring
- Retry on failure

**Processing**: svs-raw-api (Python)
- RAW file parsing
- DNG development
- JPG export

**API Layer**: Python 3.9+
- psycopg2 for DB access
- Type hints throughout
- Comprehensive error handling

**Orchestration**: Python scripts + cron
- Transfer orchestration
- Processing orchestration
- Daily pipeline execution

**Monitoring**: PostgreSQL views + Python scripts
- Analytics views
- Error tracking
- Performance metrics

---

## Document Control

**Version**: 1.0  
**Last Updated**: 2024-12-12  
**Next Review**: After Phase 1 implementation  
**Approval Required**: Yes (before production deployment)  

**Change Log**:
- v1.0 (2024-12-12): Initial requirements document
