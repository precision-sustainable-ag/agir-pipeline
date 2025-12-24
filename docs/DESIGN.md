## 1. Intro

The goal is to replace the current SemiF-Preprocessing system with something that is more modular, simple, scalable, and auditable in a way that allows us to identify gaps and monitor the current status.

## 2. Problem Summary

The existing AgIR pipeline (SemiF-Preprocessing):

- Is not sufficently unified or modular
- Relies on scripts loosely coordinated by hydra
- Lacks any type of state tracking and only uses file-based logging
- Writes outputs to JSON files that are then curated into a databases later
- Is difficult to deploy across different environments (only on SUNNY now)
- Will be difficult to scale well as datasets, BBots, or projects increase

It is difficult to determine:

- What data exists and where
- What processing has been attempted or failed
- What remains to be processed
- Which outputs are authoritative
- Is the data consistent

## 3. Design Goals

- Modular, unified, and end-to-end that supports current and future BBot imagery.
- Deployable across various environments (HPC, SciNet, SUNNY, Lightning, other on-prem servers)
- Establish the postgres cluster as a keystone system comppnent that supports both the pipeline state tracking and direct storage of resulting data products (detections, segmentation, species, cutout properties).
- Clear separation between image processing CLIs and database apis
- Keep image processing tasks as simple and as narrow scoped as possible and keep them independent of database implementation, same goes for the db api.
- Auditable: state tracking, structured logging, recording inputs, outputs, stages, failures
- Automated discovery of new batches
- Automated discovery of batches that need to start or resume processing.
- Standardize dependency management and configuration (models, color matrices, config files)

## 4. Scope and Non-Goals

### In Scope

- Batch-based image processing
- Detection, segmentation, and cutout generation
- Pipeline state tracking and provenance
- Data gap detection
- Data transfer

### Out of Scope

- Model training or development
- Real-time image processing
- Research/Plot Experimental design tracking
- Trait-based or image-based phenotyping targets
- User-facing GUIs or viz tools
- Camera configuration and calibration
- Backward compatibility with SemiF-Preprocessing
- Color matrix development
- Field Data (but likely in the future)

## 5. Architectural Principles

### Modular Pipeline Stages

Each pipeline stage is an independent executable unit with a clearly defined contract:

- Explicit inputs
- Explicit outputs
- Deterministic behavior
- Idempotent and resumable

Stages can be tested, deployed, and replaced independently.

### Clear Separation of Pipeline Parts

Image processing stages do not:

- Query the database
- Decide what runs next

The database API (agir-db):

- takes care of file inventory by indexing storge locations and transfering data from NCSU to JUNO
- ingests data products, maintains pipeline state, exposed "readiness" or backlog queries

Orchestration logic:

- Determines what work runs and when
- Manages stage failures and retries

### DB as Source of Truth

The PostgreSQL cluster is the authoritative system for:

- File inventory (`processed.globus_file_index`)
- Pipeline state (`logs` and `report`)
- Data products (image, detection, and cutout metadata, along with path pointers) (`processed`)
- Provenance metadata (`registry` and `ops`)
- Releases (`release`)
- See the DB_OVERVIEW.md for more details.

## 6. Assumptions
- Globus file index is source of truth
- Processing is performed post collection, not real-time
- NCSU “lockers” are temporary staging location.
- Some type of checksum system for understanindg that all the data has been moved from the BBot to the NCSU staging area
- BBot provides reliable xyz  image coordinates and species mapping (V3.5)
- Accomodating both BBot V3.0, V3.1 and V3.5
- Reconstruction is optional but certain criteria need to be met for it to occur (overlapping images, markers, etc.)
- pretrained models are available for detection and segmentation
- globus-cli is the main mechanism for data transfer
- QC at this stage only include schema checks, data integrity checks, and basic queryable data stats.

## 7. High - Level Solution

The pipeline is stage-bases, coordindated by an Orchestrator, and backed by the db managed by it's own api.
1. stages
2. orchestrator
3. DB (postgres)
4. DB api

- View the `PIPELINE_DIAGRAM.md` to visualize the workflow.

### Conceptual Flow

- Orchestrator, via the DB api, updates the file inventory in JUNO (trasnfers physical data and updates `source.globus_file_index`)
- Orchestrator queries for readiness
- Orchestrator begins eligible processing stage
- Stage output artifacts, report, and manifest are written to temporary storage
- DB api ingests outputs and state status from report and manifest
- Artifacts are transferred to JUNO LTS
- agir-db ingests transfer states
- DB becomes updated source of truth

## 8. Pipeline Stages

### Examples include:

- `raw_to_jpg`
- `jpg_to_det`
- `det_to_seg`
- `seg_to_cutouts`

### Stage Characteristics

- Standalone CLI (optionally containerized)
- Operates at batch or image level
- Produces:
  - Physical artifacts (images, masks, cutouts)
  - `run_report.json`
  - `manifest.json` (per-image detail)

### Stage Responsibilities

- Performs a single, narrowly scoped task
- Report execution results
- Exits with standardized exit codes

### Stage Limitations

Stages must not:

- Query or ingest into the database
- Determine pipeline readiness
- Manage retries or scheduling

## 9. Database API Responsibilities (agir-db)

agir-db is in charge of 2 main responsibilities:

### a. Acts as System of record

- Pipeline execution tracking
- Readiness and backlog queries
- Ingestion of derived data products (metadata and artifact pointers)
- Provenance tracking (models, configs, versions)
- Support for curated dataset releases

### b. Inventory/gap engine

- Identifies new batches in NCSU staging area
- Transfers data from NCSU to JUNO
- Logs transfer tracking
- Updates global file inventory

The database answers key operational questions:

- What data exists and where?
- What processing has been attempted?
- What succeeded or failed?
- What remains to be done?
- What constitutes an official release?

## 10. Orchestrator

### Role

- Queries readiness/backlog tables
- Claims a lease so two orchestrators don’t run the same stage on the same batch.
- Launches stages with resolved deps
- Launches pipeline stages
- Handles retries and resumes using exit codes
- Submit run bundle for ingestion (run_report + manifest to DB/API).
  
### Limitations

The Orchestrator does not:

- transfer data
- validate artifacts deeply (maybe a "file exists" but that's it)
- update states directly in the DB tables
- perform image processing
- ingest data into the DB including inventory correctness

### Implementation Strategy

- Initial: Slurm + Python/Bash scripts
- Future: Evaluate workflow engines (Airflow, Nextflow, Snakemake)

### Simplified Orchestrator roles
1. Asks DB, "what can I run?"
2. Claims the work
3. Launches the stage
4. Observes completetion
5. Submits the results

# 11. Configuration and Dependency Management

All dependencies (models, configs, color matrices) are:

- Versioned
- Stored in a centralized dependency store

A dedicated registry schema:

- Catalogs dependencies
- Records versions, checksums, and identifiers
- Manages selection and provenance

Dependencies are stored in a central location (perhaps semifield-utils)
