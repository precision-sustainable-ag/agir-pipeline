## High-Level Design Overview

### Terms
1. **Batch** - group of images processed together, usually by a collection event and location (`MD_2025-01-01`)
2. **Stage worker** - image processing stage (`raw_to_jpg`, `jpg_to_det`, `meta_to_cutout`, `cutout_props`, etc.)
3. **Artifacts** - processing data products (processed jpgs, bounding box detections, segmentaion masks, etc.)
4. **`run_report`** - required json file summary creted by every stage that describes execution details like status, counts, provenance (config/model/deps), and output locations. They are an important primary ingestion contract for each stage.


### Simplified
```mermaid
sequenceDiagram
    autonumber
    participant Orch as Orchestrator
    participant AGIR as agir-db API
    participant DB as Postgres DB
    participant TMP as Temp Storage
    participant ST as JUNO Storage
    participant NCSU as NCSU Lockers
    participant Stage as Stage Worker

    Orch->>NCSU: Update file inventory 
    Orch->>AGIR: query readiness
    AGIR->>DB: read pipeline state
    DB-->>AGIR: state results
    AGIR-->>Orch: ready work list

    Orch->>Stage: run stage
    Stage->>TMP: write artifacts + run_report

    Orch->>AGIR: ingest run_report
    AGIR->>DB: record run + artifacts
    AGIR->>ST: transfer artifacts
    AGIR->>DB: update locations
```

### Detailed
```mermaid
sequenceDiagram
    autonumber
    participant Orch as Orchestrator
    participant AGIR as agir-db API
    participant DB as Postgres DB
    participant TMP as Temp Storage
    participant ST as JUNO Storage
    participant NCSU as NCSU Lockers
    participant S1 as Stage 1 Worker
    participant S3 as Stage N Worker

    Note over Orch,DB: Periodic refresh to keep file inventory current

    Orch->>NCSU: identify new batches (scheduled)
    NCSU->>ST: transfer new batches
    AGIR->>DB: update logs.transfers

    Orch->>AGIR: refresh file index (scheduled)
    AGIR->>DB: update source.globus_file_index
    DB-->>AGIR: index updated
    AGIR-->>Orch: index refresh status


    loop Pipeline control loop
        Orch->>AGIR: query readiness/backlog for next eligible work
        AGIR->>DB: compute readiness from file index + prior runs + artifacts
        DB-->>AGIR: ready work units + required stage order
        AGIR-->>Orch: work list (batch/image ids)

        Orch->>AGIR: resolve active config/deps for Stage 1 (registry)
        AGIR->>DB: lookup registry active selections (config_id, model_id, deps_id)
        DB-->>AGIR: resolved IDs + artifact URIs
        AGIR-->>Orch: resolved config + dependency pointers

        Orch->>S1: run Stage 1 with resolved inputs + deps
        S1->>ST: read inputs
        S1->>TMP: write artifacts
        S1->>TMP: write run_report (and optional manifest)

        Orch->>AGIR: ingest Stage 1 run_report
        AGIR->>DB: record stage run + artifact metadata + status
        AGIR->>TMP: validate artifacts and metadata
        AGIR->>ST: promote artifacts to JUNO
        AGIR->>DB: update artifact locations and state

        Orch->>AGIR: resolve active config/deps for Stage 2 (registry)
        AGIR->>DB: lookup registry active selections
        DB-->>AGIR: resolved IDs + artifact URIs
        AGIR-->>Orch: resolved config + dependency pointers

        Orch->>AGIR: resolve active config/deps for Stage N (registry)
        AGIR->>DB: lookup registry active selections
        DB-->>AGIR: resolved IDs + artifact URIs
        AGIR-->>Orch: resolved config + dependency pointers

        Orch->>S3: run Stage N with resolved inputs + deps
        S3->>ST: read inputs
        S3->>TMP: write artifacts
        S3->>TMP: write run_report (and optional manifest)

        Orch->>AGIR: ingest Stage N run_report
        AGIR->>DB: record stage run + artifact metadata + status
        AGIR->>TMP: validate artifacts and metadata
        AGIR->>ST: transfer artifacts to JUNO
        AGIR->>DB: update artifact locations and state
    end
```
